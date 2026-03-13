/**
 * Privy 연동 모듈 — Copy Perp
 * 
 * PRIVY_APP_ID 있으면 → 실제 소셜 로그인 (React 없이 iframe 방식)
 * PRIVY_APP_ID 없으면 → 데모 모드 (지갑 주소 직접 입력)
 * 
 * 실제 배포 시:
 *   1. https://dashboard.privy.io 에서 앱 생성
 *   2. App ID를 window.PRIVY_APP_ID 에 주입
 *   3. Allowed Origins에 도메인 추가
 * 
 * Privy 공식 문서: https://docs.privy.io
 * React SDK: @privy-io/react-auth@3.17.0
 * JS SDK Core: @privy-io/js-sdk-core@0.60.5
 */

const PRIVY_APP_ID = window.PRIVY_APP_ID || '';
const PRIVY_BASE_URL = 'https://auth.privy.io';

// ── 상태 ────────────────────────────────────────
let _privyUser = null;
let _privyWallet = null;  // { address, chainType: 'solana' }
let _privyIframe = null;
let _onLoginCallback = null;

// ── 유틸 ────────────────────────────────────────
function _generateNonce() {
  return Math.random().toString(36).substring(2) + Date.now().toString(36);
}

// ── Privy iframe 팝업 방식 ───────────────────────
/**
 * Privy 로그인 팝업 열기
 * App ID 있으면 실제 Privy 팝업, 없으면 데모 모달
 */
async function privyLogin(provider = 'google', onSuccess = null) {
  if (onSuccess) _onLoginCallback = onSuccess;

  if (!PRIVY_APP_ID) {
    // 데모 모드: 지갑 주소 직접 입력
    _showDemoLoginModal();
    return;
  }

  // 실제 Privy 로그인 (iframe 팝업)
  _openPrivyPopup(provider);
}

function _openPrivyPopup(provider) {
  const nonce = _generateNonce();
  const params = new URLSearchParams({
    app_id:   PRIVY_APP_ID,
    provider: provider,
    nonce:    nonce,
    chain:    'solana',
    embedded_wallets: 'true',
  });

  const popup = window.open(
    `${PRIVY_BASE_URL}/oauth/login?${params}`,
    'privy-login',
    'width=480,height=640,scrollbars=yes'
  );

  // 팝업에서 메시지 수신
  const handleMessage = (event) => {
    if (!event.origin.includes('privy.io') && !event.origin.includes('localhost')) return;

    const { type, data } = event.data || {};
    if (type === 'privy:login:success' || type === 'privy:authenticated') {
      window.removeEventListener('message', handleMessage);
      popup?.close();

      const user = data?.user || data;
      const wallet = _extractSolanaWallet(user);
      _onLoginSuccess(user, wallet, provider);
    } else if (type === 'privy:login:error') {
      window.removeEventListener('message', handleMessage);
      popup?.close();
      console.error('[Privy] 로그인 오류:', data);
    }
  };

  window.addEventListener('message', handleMessage);

  // 팝업 닫힘 감지 (취소)
  const checkClosed = setInterval(() => {
    if (popup?.closed) {
      clearInterval(checkClosed);
      window.removeEventListener('message', handleMessage);
    }
  }, 500);
}

function _extractSolanaWallet(user) {
  if (!user) return null;
  const accounts = user.linked_accounts || user.linkedAccounts || [];
  const solana = accounts.find(a =>
    (a.type === 'wallet' || a.wallet_client_type === 'privy') &&
    (a.chain_type === 'solana' || a.chainType === 'solana')
  );
  return solana ? { address: solana.address || solana.public_key, chainType: 'solana' } : null;
}

function _onLoginSuccess(user, wallet, provider) {
  _privyUser   = user;
  _privyWallet = wallet;

  const address = wallet?.address || user?.id || 'demo-' + _generateNonce();
  console.log('[Privy] 로그인 성공:', { provider, address });

  if (_onLoginCallback) {
    _onLoginCallback({ user, wallet, address, provider });
    _onLoginCallback = null;
  }
}

// ── 데모 모달 (App ID 없을 때) ──────────────────
function _showDemoLoginModal() {
  // 기존 demo-login-modal 제거
  document.getElementById('demo-login-modal')?.remove();

  const modal = document.createElement('div');
  modal.id = 'demo-login-modal';
  modal.style.cssText = `
    position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:9999;
    display:flex;align-items:center;justify-content:center;
    backdrop-filter:blur(4px);
  `;
  modal.innerHTML = `
    <div style="background:#0d1425;border:1px solid #1a2640;border-radius:16px;padding:32px;width:100%;max-width:420px;">
      <div style="font-size:20px;font-weight:800;margin-bottom:8px;">Demo 모드 로그인</div>
      <div style="font-size:13px;color:#64748b;margin-bottom:20px;">
        실제 배포 시 Privy 소셜 로그인으로 교체됩니다.<br>
        Solana 지갑 주소를 입력하거나 데모 계정을 사용하세요.
      </div>
      <div style="margin-bottom:16px;">
        <label style="font-size:12px;color:#94a3b8;display:block;margin-bottom:6px;">Solana 지갑 주소</label>
        <input id="demo-wallet-input" type="text"
          placeholder="예: 3AHZqroc... (비워두면 테스트 계정 사용)"
          style="width:100%;background:#080c18;border:1px solid #1a2640;color:#e2e8f0;
                 padding:10px 12px;border-radius:8px;font-size:13px;outline:none;" />
      </div>
      <div style="display:flex;gap:10px;margin-bottom:12px;">
        <button onclick="window._privyDemoLogin('custom')" style="
          flex:1;background:#00d4ff;color:#080c18;border:none;padding:12px;
          border-radius:8px;font-weight:700;cursor:pointer;font-size:14px;">
          연결하기
        </button>
        <button onclick="window._privyDemoLogin('demo')" style="
          flex:1;background:#1a2640;color:#e2e8f0;border:1px solid #1a2640;padding:12px;
          border-radius:8px;font-weight:700;cursor:pointer;font-size:14px;">
          데모 계정 사용
        </button>
      </div>
      <button onclick="document.getElementById('demo-login-modal').remove()" style="
        width:100%;background:transparent;border:none;color:#64748b;
        font-size:13px;cursor:pointer;padding:8px;">취소</button>
      <div style="margin-top:16px;padding:12px;background:#080c18;border-radius:8px;font-size:11px;color:#64748b;">
        💡 실제 소셜 로그인을 위해 <strong style="color:#00d4ff">PRIVY_APP_ID</strong>를 설정하세요.<br>
        발급: <a href="https://dashboard.privy.io" target="_blank" style="color:#00d4ff">dashboard.privy.io</a>
      </div>
    </div>
  `;

  document.body.appendChild(modal);
  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.remove();
  });
}

window._privyDemoLogin = function(type) {
  const DEMO_ADDR = '3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ';
  const inputEl   = document.getElementById('demo-wallet-input');
  const address   = (type === 'custom' && inputEl?.value.trim())
    ? inputEl.value.trim()
    : DEMO_ADDR;

  document.getElementById('demo-login-modal')?.remove();

  const fakeUser   = { id: address, linked_accounts: [] };
  const fakeWallet = { address, chainType: 'solana' };
  _onLoginSuccess(fakeUser, fakeWallet, 'demo');
};

// ── 지갑 서명 (Builder Code 승인용) ─────────────
/**
 * Privy embedded wallet으로 메시지 서명
 * 
 * 실제 구현 시:
 *   const { signMessage } = usePrivy();
 *   const sig = await signMessage({ message });
 * 
 * 현재: 데모 모드 (서버 측 Agent Key로 대체)
 */
async function privySignMessage(message) {
  if (!_privyWallet || !PRIVY_APP_ID) {
    // 서버 측 Agent Key로 서명 (데모/개발용)
    console.log('[Privy] signMessage — 서버 측 서명 사용 (App ID 미설정)');
    return null;  // 서버가 AGENT_PRIVATE_KEY로 서명
  }

  // Privy REST API를 통한 서명
  // POST https://auth.privy.io/api/v1/wallets/{address}/rpc
  try {
    const resp = await fetch(`${PRIVY_BASE_URL}/api/v1/wallets/${_privyWallet.address}/rpc`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'privy-app-id': PRIVY_APP_ID,
        'Authorization': `Bearer ${_privyUser?.access_token || ''}`,
      },
      body: JSON.stringify({
        method:  'signMessage',
        params:  { message },
        caip2:   'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp',  // Solana mainnet
      }),
    });
    const data = await resp.json();
    return data?.data?.signature || null;
  } catch (e) {
    console.error('[Privy] signMessage 실패:', e);
    return null;
  }
}

// ── 현재 연결된 지갑 ─────────────────────────────
function getPrivyWallet() {
  return _privyWallet;
}

function getPrivyUser() {
  return _privyUser;
}

function isPrivyConnected() {
  return !!_privyWallet?.address;
}

// ── Builder Code 승인 전체 플로우 ────────────────
/**
 * 팔로워 온보딩 시 Builder Code 승인 자동 실행
 * 
 * 1. Privy signMessage()로 서명 (또는 서버 측 서명)
 * 2. POST /followers/onboard (서버가 approve API 호출)
 * 3. 성공/실패와 무관하게 팔로우 등록
 */
async function privyApproveBuilderCode(followerAddress) {
  const sig = await privySignMessage(
    JSON.stringify({
      type:          'approve_builder_code',
      builder_code:  'noivan',
      max_fee_rate:  '0.001',
      timestamp:     Date.now(),
    })
  );

  // 서명이 있으면 온보딩 payload에 포함
  // 서명 없으면 서버가 Agent Key로 처리
  return { follower_address: followerAddress, signature: sig };
}

// ── 내보내기 ─────────────────────────────────────
window.PrivyIntegration = {
  login:              privyLogin,
  signMessage:        privySignMessage,
  getWallet:          getPrivyWallet,
  getUser:            getPrivyUser,
  isConnected:        isPrivyConnected,
  approveBuilderCode: privyApproveBuilderCode,
  APP_ID:             PRIVY_APP_ID,
  IS_DEMO_MODE:       !PRIVY_APP_ID,
};

console.log(`[Privy] 초기화 — ${PRIVY_APP_ID ? '실제 모드 (App ID: ' + PRIVY_APP_ID.substring(0,8) + '...)' : '데모 모드'}`);
