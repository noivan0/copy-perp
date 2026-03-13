/**
 * Privy 연동 모듈 — Copy Perp (완전 구현 버전)
 *
 * 모드 1 (실제): PRIVY_APP_ID 있을 때
 *   - Privy CDN (privy.min.js) 로드
 *   - Google/Twitter 소셜 로그인 → Solana 임베디드 지갑 자동 생성
 *   - signMessage() → Builder Code approve 서명
 *   - 지갑 주소 → /followers/onboard 자동 호출
 *
 * 모드 2 (데모): PRIVY_APP_ID 없을 때
 *   - 지갑 주소 직접 입력
 *   - 서버 측 Agent Key로 서명 (백엔드 처리)
 *
 * 공식 문서: https://docs.privy.io
 * SDK: @privy-io/react-auth (React용), privy.min.js (Vanilla JS용)
 */

'use strict';

// ── 설정 ─────────────────────────────────────────
const PRIVY_APP_ID      = window.PRIVY_APP_ID || '';
const PRIVY_CDN_URL     = 'https://cdn.privy.io/sdk/latest/privy.min.js';
const API_BASE          = window.API_BASE || 'http://localhost:8001';
const BUILDER_CODE      = 'noivan';
const BUILDER_FEE_RATE  = '0.001';

// ── 상태 ────────────────────────────────────────
let _privy        = null;   // Privy SDK 인스턴스
let _user         = null;   // 현재 로그인 유저
let _wallet       = null;   // { address, chainType: 'solana', walletClientType }
let _callbacks    = [];     // 로그인 성공 콜백

// ── Privy SDK 로드 ──────────────────────────────
async function _loadPrivySDK() {
  if (_privy || !PRIVY_APP_ID) return null;

  return new Promise((resolve, reject) => {
    // 이미 로드됐으면 스킵
    if (window.Privy) {
      _initPrivy();
      resolve(_privy);
      return;
    }
    const script = document.createElement('script');
    script.src  = PRIVY_CDN_URL;
    script.async = true;
    script.onload = () => {
      _initPrivy();
      resolve(_privy);
    };
    script.onerror = reject;
    document.head.appendChild(script);
  });
}

function _initPrivy() {
  if (!window.Privy || _privy) return;
  _privy = new window.Privy({
    appId: PRIVY_APP_ID,
    config: {
      loginMethods: ['google', 'twitter', 'email', 'wallet'],
      appearance: { theme: 'dark', accentColor: '#6366f1' },
      embeddedWallets: {
        solana: { createOnLogin: 'users-without-wallets' },
      },
    },
    onSuccess: _onPrivyLogin,
  });
  console.log('[Privy] SDK 초기화 완료 (App ID:', PRIVY_APP_ID.slice(0, 8) + '...)');
}

// ── 로그인 성공 콜백 ────────────────────────────
async function _onPrivyLogin(user) {
  _user = user;

  // Solana 임베디드 지갑 추출 (임베디드 우선)
  const accounts = user?.linkedAccounts || [];
  _wallet =
    accounts.find(a => a.type === 'wallet' && a.chainType === 'solana' && a.walletClientType === 'privy') ||
    accounts.find(a => a.type === 'wallet' && a.chainType === 'solana') ||
    null;

  const address = _wallet?.address;
  console.log('[Privy] 로그인 성공:', address?.slice(0, 12) + '...');

  if (address) {
    // Builder Code 승인 + 팔로워 온보딩 자동 실행
    await _autoOnboard(address);
  }

  // 등록된 콜백 실행
  _callbacks.forEach(fn => { try { fn({ user, address }); } catch (e) {} });
}

// ── 자동 온보딩 플로우 ────────────────────────────
/**
 * 로그인 완료 후 자동 실행:
 * 1. (선택) Privy signMessage → Builder Code approve 서명 생성
 * 2. POST /followers/onboard → 서버가 approve API 호출 + DB 등록
 */
async function _autoOnboard(address) {
  try {
    // Builder Code 서명 시도 (실패해도 온보딩 계속)
    let signature = null;
    try {
      signature = await _signBuilderCodeMessage(address);
    } catch (e) {
      console.warn('[Privy] Builder Code 서명 실패 (서버 측 처리로 대체):', e.message);
    }

    const payload = {
      follower_address:   address,
      copy_ratio:         0.10,
      max_position_usdc:  50.0,
      privy_user_id:      _user?.id,
    };
    if (signature) payload.client_signature = signature;

    const resp = await fetch(`${API_BASE}/followers/onboard`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
    });
    const data = await resp.json();
    console.log('[Privy] 온보딩 결과:', data.ok ? '✅ 성공' : '❌ 실패', data.note || '');
    return data;
  } catch (e) {
    console.error('[Privy] 온보딩 오류:', e);
    return { ok: false, error: e.message };
  }
}

// ── Builder Code 서명 ────────────────────────────
/**
 * Privy embedded wallet signMessage로 Builder Code approve 서명 생성
 * 공식 Pacifica 서명 구조:
 * {
 *   "timestamp": <ms>,
 *   "expiry_window": 5000,
 *   "type": "approve_builder_code",
 *   "data": { "builder_code": "noivan", "max_fee_rate": "0.001" }
 * }
 */
async function _signBuilderCodeMessage(address) {
  if (!_privy || !PRIVY_APP_ID) return null;

  const timestamp = Date.now();
  const payload = {
    timestamp,
    expiry_window: 5000,
    type: 'approve_builder_code',
    data: { builder_code: BUILDER_CODE, max_fee_rate: BUILDER_FEE_RATE },
  };

  // 재귀적 키 정렬 (Pacifica 서명 규칙)
  function sortKeys(obj) {
    if (typeof obj !== 'object' || obj === null) return obj;
    if (Array.isArray(obj)) return obj.map(sortKeys);
    return Object.fromEntries(
      Object.keys(obj).sort().map(k => [k, sortKeys(obj[k])])
    );
  }
  const compact = JSON.stringify(sortKeys(payload));

  // Privy Server Wallets API (REST) 방식
  // POST https://auth.privy.io/api/v1/wallets/{address}/rpc
  const resp = await fetch(`https://auth.privy.io/api/v1/wallets/${address}/rpc`, {
    method: 'POST',
    headers: {
      'Content-Type':  'application/json',
      'privy-app-id':  PRIVY_APP_ID,
      'Authorization': `Bearer ${_user?.access_token || ''}`,
    },
    body: JSON.stringify({
      method: 'signMessage',
      params: { message: compact },
      caip2:  'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp',
    }),
  });

  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`Privy signMessage HTTP ${resp.status}: ${err.slice(0, 100)}`);
  }

  const result = await resp.json();
  return result?.data?.signature || null;
}

// ── 데모 모드 (App ID 없을 때) ───────────────────
function _showDemoLoginModal() {
  // 기존 모달 제거
  document.getElementById('privy-demo-modal')?.remove();

  const modal = document.createElement('div');
  modal.id = 'privy-demo-modal';
  modal.style.cssText = `
    position:fixed; inset:0; background:rgba(0,0,0,.6); display:flex;
    align-items:center; justify-content:center; z-index:9999;
  `;
  modal.innerHTML = `
    <div style="background:#1f2937; border:1px solid #374151; border-radius:16px;
                padding:32px; width:380px; max-width:92vw; font-family:sans-serif;">
      <h2 style="color:#fff; font-size:20px; margin:0 0 8px">데모 모드</h2>
      <p style="color:#9ca3af; font-size:13px; margin:0 0 20px">
        Privy App ID 미설정 — 지갑 주소를 직접 입력하세요.<br>
        실 배포 시 Google 로그인으로 자동 전환됩니다.
      </p>
      <input id="privy-demo-addr" type="text"
        placeholder="Solana 지갑 주소 (예: 3AHZqroc...)"
        style="width:100%; box-sizing:border-box; padding:10px 12px; border-radius:8px;
               border:1px solid #4b5563; background:#111827; color:#fff; font-size:13px;
               font-family:monospace; outline:none; margin-bottom:12px;" />
      <button onclick="
        const addr = document.getElementById('privy-demo-addr').value.trim();
        if (!addr || addr.length < 32) { alert('유효한 Solana 주소를 입력하세요'); return; }
        document.getElementById('privy-demo-modal').remove();
        window.PrivyIntegration._demoLogin(addr);
      " style="width:100%; padding:12px; background:#6366f1; color:#fff; border:none;
               border-radius:8px; font-size:14px; font-weight:600; cursor:pointer;">
        시작하기
      </button>
      <button onclick="document.getElementById('privy-demo-modal').remove()"
              style="width:100%; padding:10px; background:transparent; color:#6b7280;
                     border:none; font-size:13px; cursor:pointer; margin-top:8px;">
        취소
      </button>
    </div>
  `;
  document.body.appendChild(modal);
  setTimeout(() => document.getElementById('privy-demo-addr')?.focus(), 100);
}

async function _demoLogin(address) {
  _user   = { id: 'demo:' + address, demo: true };
  _wallet = { address, chainType: 'solana', walletClientType: 'demo' };
  await _autoOnboard(address);
  _callbacks.forEach(fn => { try { fn({ user: _user, address }); } catch (e) {} });
}

// ── 공개 API ─────────────────────────────────────
/**
 * 로그인 시작
 * @param {string} provider - 'google' | 'twitter' | 'email' | 'wallet'
 * @param {Function} onSuccess - 성공 콜백 fn({ user, address })
 */
async function privyLogin(provider = 'google', onSuccess = null) {
  if (onSuccess) _callbacks.push(onSuccess);

  if (!PRIVY_APP_ID) {
    _showDemoLoginModal();
    return;
  }

  try {
    await _loadPrivySDK();
    if (_privy) {
      await _privy.login({ loginMethods: [provider] });
    } else {
      console.error('[Privy] SDK 로드 실패 → 데모 모드로 대체');
      _showDemoLoginModal();
    }
  } catch (e) {
    console.error('[Privy] 로그인 실패:', e);
    _showDemoLoginModal();
  }
}

async function privyLogout() {
  _user = null; _wallet = null;
  try { await _privy?.logout(); } catch (e) {}
  console.log('[Privy] 로그아웃');
}

function privyGetWallet()     { return _wallet; }
function privyGetUser()       { return _user; }
function privyIsConnected()   { return !!_wallet?.address; }

/**
 * Builder Code approve 서명 생성
 * Privy SDK 있으면 embedded wallet 서명, 없으면 서버 측 처리
 */
async function privyApproveBuilderCode(followerAddress) {
  const sig = await _signBuilderCodeMessage(followerAddress).catch(() => null);
  return { follower_address: followerAddress, signature: sig };
}

// ── 내보내기 ─────────────────────────────────────
window.PrivyIntegration = {
  login:              privyLogin,
  logout:             privyLogout,
  getWallet:          privyGetWallet,
  getUser:            privyGetUser,
  isConnected:        privyIsConnected,
  approveBuilderCode: privyApproveBuilderCode,
  onboard:            _autoOnboard,
  _demoLogin,          // 내부용
  APP_ID:             PRIVY_APP_ID,
  IS_DEMO_MODE:       !PRIVY_APP_ID,
};

// 자동 초기화 (App ID 있으면 SDK 미리 로드)
if (PRIVY_APP_ID) {
  _loadPrivySDK().catch(e => console.warn('[Privy] 사전 로드 실패:', e));
}

console.log(
  `[Privy] 초기화 — ${PRIVY_APP_ID
    ? '실제 모드 (App ID: ' + PRIVY_APP_ID.slice(0, 8) + '...)'
    : '데모 모드 (PRIVY_APP_ID 미설정)'}`
);
