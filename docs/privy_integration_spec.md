# Privy 연동 완전 스펙

**작성일:** 2026-03-14  
**버전:** v2 (scrapling 실문서 기반)  
**참조:** https://docs.privy.io  
**React-auth 버전:** 3.17.0

---

## 1. 개요

Privy = Auth + Embedded Wallet SDK. 소셜 로그인만으로 Solana 지갑을 자동 생성해주며, 유저가 직접 Private Key를 관리할 필요 없다.

### Copy Perp에서의 역할
| 단계 | Privy 역할 |
|------|-----------|
| 1. 로그인 | Google/Twitter/Discord/지갑 연결 |
| 2. 지갑 생성 | Embedded Solana 지갑 자동 생성 |
| 3. Builder Code 승인 | `signMessage()` → Pacifica approve_builder_code |
| 4. 지속 | 이후 복사 주문 모두 builder_code 포함 → 수수료 수취 |

---

## 2. App ID 발급

1. https://dashboard.privy.io 접속
2. **Create new app** → 앱 이름: `Copy Perp`
3. **Chains** 탭 → **Solana** 체크 (필수)
4. **Login Methods** → Google, Twitter, Discord, Wallet 활성화
5. **Embedded Wallets** → `Create on login: users-without-wallets` 설정
6. **Allowed origins** 추가:
   - `https://copy-perp.vercel.app`
   - `http://localhost:8001`
7. App ID 복사 (`clxxxxxx...` 형식)

```env
PRIVY_APP_ID=clxxxxxxxxxxxxxxxxxx
NEXT_PUBLIC_PRIVY_APP_ID=clxxxxxxxxxxxxxxxxxx
```

> **가격:** MAU 1,000명까지 무료 (https://privy.io/pricing)

---

## 3. React SDK (권장) vs Vanilla JS

### 3-A. React SDK 방식

```bash
npm install @privy-io/react-auth @solana/web3.js
```

**`_app.tsx` 설정:**

```tsx
import { PrivyProvider } from '@privy-io/react-auth';
import { toSolanaWalletConnectors } from '@privy-io/react-auth/solana';

export default function App({ Component, pageProps }) {
  return (
    <PrivyProvider
      appId={process.env.NEXT_PUBLIC_PRIVY_APP_ID!}
      config={{
        loginMethods: ['google', 'twitter', 'discord', 'wallet'],
        embeddedWallets: {
          createOnLogin: 'users-without-wallets',  // 지갑 없는 유저 자동 생성
          requireUserPasswordOnCreate: false,        // 비밀번호 없이 생성
        },
        externalWallets: {
          solana: {
            connectors: toSolanaWalletConnectors(),  // Phantom/Backpack 지원
          },
        },
        appearance: {
          theme: 'dark',
          accentColor: '#00d4ff',  // Copy Perp 브랜드 컬러
        },
      }}
    >
      <Component {...pageProps} />
    </PrivyProvider>
  );
}
```

**지갑 연결 + 주소 가져오기:**

```tsx
import { usePrivy, useSolanaWallets } from '@privy-io/react-auth';

function WalletButton() {
  const { login, ready, authenticated } = usePrivy();
  const { wallets } = useSolanaWallets();

  // Embedded 지갑 우선, 없으면 External(Phantom 등)
  const solanaWallet = wallets.find(w => w.walletClientType === 'privy')
    ?? wallets.find(w => w.chainType === 'solana');

  if (!authenticated) {
    return <button onClick={login}>Connect Wallet</button>;
  }

  return <span>{solanaWallet?.address}</span>;
}
```

### 3-B. Vanilla JS 방식 (현재 Copy Perp 프론트)

Privy는 React SDK 위주 — Vanilla JS에선 **외부 지갑(Phantom/Backpack)**만 직접 지원.

```html
<!-- Solana Web3.js CDN -->
<script src="https://unpkg.com/@solana/web3.js@latest/lib/index.iife.min.js"></script>

<script>
// Phantom 지갑 연결
async function connectWallet() {
  if (!window.solana?.isPhantom) {
    window.open('https://phantom.app', '_blank');
    return;
  }
  const { publicKey } = await window.solana.connect();
  return publicKey.toString();  // Solana 주소
}

// Backpack 지갑
async function connectBackpack() {
  if (!window.backpack) return null;
  await window.backpack.connect();
  return window.backpack.publicKey.toString();
}
</script>
```

> **Privy Embedded Wallet Vanilla 지원 여부:** 공식 미지원.  
> 대안: `@privy-io/react-auth` + Next.js 적용 권장.  
> 현재 프론트: Phantom/Backpack 직접 연결 구현 완료.

---

## 4. Solana signMessage() 완전 구현

### 4-A. React + Privy Embedded Wallet

```tsx
import { useSolanaWallets } from '@privy-io/react-auth';

function useBuilderCodeApproval() {
  const { wallets } = useSolanaWallets();

  const approveBuilderCode = async (
    builderCode = 'noivan',
    maxFeeRate = '0.001'
  ): Promise<{ ok: boolean; signature?: string; error?: string }> => {
    // 1. Solana 지갑 선택 (embedded 우선)
    const wallet = wallets.find(w => w.walletClientType === 'privy')
      ?? wallets.find(w => w.chainType === 'solana');
    if (!wallet) return { ok: false, error: 'Solana wallet not found' };

    // 2. Pacifica 서명 페이로드 생성
    //    규칙: 키 알파벳 정렬 → 컴팩트 JSON → UTF-8 bytes
    const sortKeys = (obj: any): any => {
      if (typeof obj !== 'object' || obj === null) return obj;
      if (Array.isArray(obj)) return obj.map(sortKeys);
      return Object.fromEntries(
        Object.keys(obj).sort().map(k => [k, sortKeys(obj[k])])
      );
    };

    const payload = {
      data: { builder_code: builderCode, max_fee_rate: maxFeeRate },
      expiry_window: 5000,
      timestamp: Date.now(),
      type: 'approve_builder_code',
    };
    const msgStr = JSON.stringify(sortKeys(payload));
    const msgBytes = new TextEncoder().encode(msgStr);

    // 3. signMessage 호출
    //    - Embedded wallet: 팝업 없이 자동 서명 ✅
    //    - External (Phantom): 지갑 확장 팝업 승인 필요
    const provider = await wallet.getProvider();
    const { signature } = await provider.signMessage(msgBytes);

    // 4. Base58 인코딩
    const bs58 = (arr: Uint8Array): string => {
      const ALPHA = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
      let d: number[] = [], s = '';
      for (let i = 0; i < arr.length; i++) {
        let c = arr[i];
        s += c === 0 && s.length === 0 ? '1' : '';
        for (let j = 0; j < d.length || c; j++) {
          const n = (d[j] ?? 0) * 256 + c;
          d[j] = n % 58; c = Math.floor(n / 58); j++;
        }
      }
      while (d.length) s += ALPHA[d.pop()!];
      return s;
    };
    const sigBase58 = bs58(new Uint8Array(signature));

    // 5. 백엔드 전송
    const res = await fetch('/builder/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...payload,
        account: wallet.address,
        signature: sigBase58,
        builder_code: builderCode,
        max_fee_rate: maxFeeRate,
      }),
    });
    const data = await res.json();
    return data.ok
      ? { ok: true, signature: sigBase58 }
      : { ok: false, error: data.error };
  };

  return { approveBuilderCode };
}
```

### 4-B. Vanilla JS + Phantom

```javascript
async function signBuilderCodeApproval(builderCode = 'noivan', maxFeeRate = '0.001') {
  const provider = window.solana;
  if (!provider) { alert('Phantom 지갑을 설치해주세요'); return null; }

  // 1. 페이로드 생성 (알파벳 정렬)
  function sortKeys(obj) {
    if (typeof obj !== 'object' || !obj) return obj;
    if (Array.isArray(obj)) return obj.map(sortKeys);
    return Object.fromEntries(Object.keys(obj).sort().map(k => [k, sortKeys(obj[k])]));
  }
  const payload = {
    data: { builder_code: builderCode, max_fee_rate: maxFeeRate },
    expiry_window: 5000,
    timestamp: Date.now(),
    type: 'approve_builder_code',
  };
  const msgStr = JSON.stringify(sortKeys(payload));
  const msgBytes = new TextEncoder().encode(msgStr);

  // 2. 서명 (Phantom 팝업)
  const { signature } = await provider.signMessage(msgBytes, 'utf8');

  // 3. Base58 인코딩
  function bs58Encode(arr) {
    const ALPHA = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
    let d = [], s = '';
    for (let i = 0; i < arr.length; i++) {
      let c = arr[i];
      s += (c === 0 && s.length === 0) ? '1' : '';
      for (let j = 0; j < d.length || c; j++) {
        const n = (d[j] || 0) * 256 + c;
        d[j] = n % 58; c = Math.floor(n / 58); j++;
      }
    }
    while (d.length) s += ALPHA[d.pop()];
    return s;
  }
  const sigBase58 = bs58Encode(new Uint8Array(signature));

  // 4. 백엔드 전송 → POST /builder/approve
  const walletAddress = provider.publicKey.toString();
  const res = await fetch(`${API_BASE}/builder/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...payload,
      account: walletAddress,
      signature: sigBase58,
      builder_code: builderCode,
      max_fee_rate: maxFeeRate,
    }),
  });
  return res.json();
}
```

---

## 5. Builder Code 승인 UX 플로우

```
[Start Copying 버튼 클릭]
        ↓
[로그인 여부 확인]
  미로그인 → 로그인 모달 (Google/Twitter/Discord/Wallet)
  로그인됨 → 계속
        ↓
[Builder Code 승인 상태 조회] GET /builder/status/{address}
  이미 승인 → 바로 팔로우 등록
  미승인 → 승인 안내 모달 표시
        ↓
[모달: "Copy Perp 수수료 최적화를 위해 한 번만 서명해주세요"]
  [서명하기] 버튼
        ↓
  Embedded Wallet → 자동 서명 (팝업 없음) ✅ UX 최적
  External (Phantom) → 지갑 팝업 서명 승인
        ↓
[POST /builder/approve] → Pacifica API 전달
        ↓
[성공] → 팔로우 등록 → 복사 시작
[실패: Builder code not found] → "Pacifica 팀 등록 대기 중" 안내
        ↓
[이후 모든 복사 주문에 builder_code='noivan' 자동 포함]
```

### UX 핵심 포인트
- **Embedded Wallet 우선**: 팝업 없이 자동 서명 → 마찰 0
- **모달 타이밍**: 첫 팔로우 시 1회만 요청
- **실패 처리**: Builder code 서버 미등록 시 조용히 스킵 (주문은 정상 실행)

---

## 6. 서버사이드 접근 (고급)

Privy는 서버에서도 유저 지갑에 접근 가능 (유저 오프라인 상태):
- 한도 주문, 자동 포트폴리오 재조정, 에이전트 트레이딩 등에 활용
- 설정: **Policies & controls** → Authorization Keys 구성 필요
- 문서: https://docs.privy.io/wallets/wallets/server-side-access

Copy Perp는 현재 서버사이드 접근 불필요 (에이전트 지갑 별도 보유).

---

## 7. 체크리스트

- [ ] dashboard.privy.io 앱 생성
- [ ] Solana 체인 활성화
- [ ] Embedded Wallets `createOnLogin: users-without-wallets` 설정
- [ ] Allowed origins 등록
- [ ] `NEXT_PUBLIC_PRIVY_APP_ID` 환경변수 설정
- [ ] React 앱이면 `PrivyProvider` 래핑
- [ ] `signBuilderCodeApproval()` 함수 연결
- [ ] `/builder/approve` API 연결 확인
- [ ] Phantom 없는 유저 → 설치 안내 처리

---

## 8. 참고 링크

| 문서 | URL |
|------|-----|
| 공식 문서 | https://docs.privy.io |
| Solana signMessage | https://docs.privy.io/wallets/using-wallets/solana/sign-a-message |
| 대시보드 | https://dashboard.privy.io |
| 가격 | https://privy.io/pricing |
| React SDK npm | https://www.npmjs.com/package/@privy-io/react-auth |
