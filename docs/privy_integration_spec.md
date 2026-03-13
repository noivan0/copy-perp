# Privy 연동 완전 스펙

**작성일:** 2026-03-14  
**대상:** Copy Perp 프론트엔드 개발팀  
**참조:** https://docs.privy.io

---

## 1. 개요

Privy는 소셜 로그인(Google/Twitter/Discord)으로 **내장형 Solana 지갑(Embedded Wallet)**을 자동 생성해주는 Auth+Wallet SDK입니다.

### Copy Perp에서의 역할
- 유저 로그인 → Solana 지갑 자동 생성
- 지갑으로 Pacifica `approve_builder_code` 서명
- 이후 모든 복사 주문에 builder_code 자동 포함

---

## 2. App ID 발급

1. https://dashboard.privy.io 접속
2. **Create new app** 클릭
3. App 이름: `Copy Perp`
4. 로그인 방식 선택: Google, Twitter, Discord, Wallet
5. **Chains 설정** → Solana 체크 (중요!)
6. **App ID** 복사 → `.env`에 `PRIVY_APP_ID=clxxxxxx` 입력
7. **Allowed Origins** 추가: `https://copy-perp.vercel.app`, `http://localhost:8001`

---

## 3. React SDK 설치 및 설정

```bash
npm install @privy-io/react-auth @solana/web3.js
```

### `_app.tsx` 설정

```tsx
import { PrivyProvider } from '@privy-io/react-auth';
import { toSolanaWalletConnectors } from '@privy-io/react-auth/solana';

export default function App({ Component, pageProps }) {
  return (
    <PrivyProvider
      appId={process.env.NEXT_PUBLIC_PRIVY_APP_ID}
      config={{
        loginMethods: ['google', 'twitter', 'discord', 'wallet'],
        embeddedWallets: {
          createOnLogin: 'users-without-wallets',  // 지갑 없는 유저 자동 생성
          requireUserPasswordOnCreate: false,
        },
        externalWallets: {
          solana: {
            connectors: toSolanaWalletConnectors(),  // Phantom 등 외부 지갑 지원
          },
        },
        appearance: {
          theme: 'dark',
          accentColor: '#00d4ff',  // Copy Perp 브랜드 컬러
          logo: 'https://copy-perp.vercel.app/logo.png',
        },
      }}
    >
      <Component {...pageProps} />
    </PrivyProvider>
  );
}
```

---

## 4. Solana 지갑 연결 및 서명 흐름

### 4-1. 로그인 + 지갑 주소 가져오기

```tsx
import { usePrivy, useSolanaWallets } from '@privy-io/react-auth';

function CopyPerpApp() {
  const { login, ready, authenticated, user } = usePrivy();
  const { wallets } = useSolanaWallets();

  // Solana 지갑 (embedded 우선, 없으면 external)
  const solanaWallet = wallets.find(w => w.walletClientType === 'privy')
    || wallets.find(w => w.chainType === 'solana');

  const walletAddress = solanaWallet?.address;  // Solana pubkey

  if (!authenticated) {
    return <button onClick={login}>Connect Wallet</button>;
  }

  return <div>Connected: {walletAddress}</div>;
}
```

### 4-2. Builder Code 승인 서명

Pacifica `approve_builder_code` 서명 구조:
1. payload 생성 → 키 알파벳 정렬 → 컴팩트 JSON
2. UTF-8 인코딩 → Ed25519 서명
3. Base58 인코딩 → API 전송

```tsx
import { useSolanaWallets } from '@privy-io/react-auth';

function useBuilderCodeApproval() {
  const { wallets } = useSolanaWallets();
  
  const approveBuilderCode = async (builderCode = 'noivan', maxFeeRate = '0.001') => {
    const wallet = wallets.find(w => w.chainType === 'solana');
    if (!wallet) throw new Error('Solana wallet not found');

    // 1. Payload 생성 (알파벳 정렬)
    const payload = {
      data: { builder_code: builderCode, max_fee_rate: maxFeeRate },
      expiry_window: 5000,
      timestamp: Date.now(),
      type: 'approve_builder_code',
    };
    
    function sortKeys(obj: any): any {
      if (typeof obj !== 'object' || obj === null) return obj;
      if (Array.isArray(obj)) return obj.map(sortKeys);
      return Object.fromEntries(
        Object.keys(obj).sort().map(k => [k, sortKeys(obj[k])])
      );
    }
    
    const msgStr = JSON.stringify(sortKeys(payload));
    const msgBytes = new TextEncoder().encode(msgStr);

    // 2. Privy signMessage (팝업 없이 embedded wallet이면 자동 서명)
    const provider = await wallet.getProvider();  // SolanaProvider
    const { signature } = await provider.signMessage(msgBytes);

    // 3. Base58 인코딩
    const bs58Encode = (arr: Uint8Array): string => {
      const ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
      let d: number[] = [], s = '';
      for (let i = 0; i < arr.length; i++) {
        let carry = arr[i];
        s += carry === 0 && s.length === 0 ? '1' : '';
        for (let j = 0; j < d.length || carry; j++) {
          const n = (d[j] || 0) * 256 + carry;
          d[j] = n % 58; carry = Math.floor(n / 58); j++;
        }
      }
      while (d.length) s += ALPHABET[d.pop()!];
      return s;
    };

    const sigBase58 = bs58Encode(signature);

    // 4. 백엔드 전송
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
    
    return res.json();
  };

  return { approveBuilderCode };
}
```

### 4-3. UX 플로우 권장 방식

```
[로그인 버튼 클릭]
        ↓
[Privy 로그인 모달 - Google/Twitter/Discord/Wallet]
        ↓
[내장 지갑 자동 생성] ← embedded wallet
        ↓
[팔로우 폼 활성화]
        ↓
[트레이더 선택 + Copy 버튼]
        ↓
[Builder Code 승인 여부 체크] → 미승인이면
        ↓
[서명 안내 모달 표시]
  "수수료 절감을 위해 한 번만 서명해주세요"
        ↓
[embedded wallet: 팝업 없이 자동 서명] ✅
[external wallet: Phantom 팝업 서명]
        ↓
[/builder/approve API 전송]
        ↓
[팔로우 등록 완료 + 복사 시작]
```

> **embedded wallet vs external wallet 서명 차이:**
> - embedded (Privy): `signMessage` 호출 시 **팝업 없이 자동** 처리 (UX 최적)
> - external (Phantom 등): 지갑 확장에서 **팝업 승인** 필요

---

## 5. Vanilla JS 환경 (현재 Copy Perp 프론트)

Privy React SDK 없이 사용할 경우:
- Phantom/Backpack 등 외부 지갑만 지원 (`window.solana`)
- `window.solana.signMessage(msgBytes)` → `{ signature: Uint8Array }`
- embedded wallet 미지원 (React 필요)

**현재 구현:** `frontend/index.html`에 `loginWithWallet()` 함수로 Phantom 직접 연결 구현 완료.

---

## 6. 환경변수 설정

```env
# .env
PRIVY_APP_ID=clxxxxxxxxxxxxxx      # Privy 대시보드에서 발급
NEXT_PUBLIC_PRIVY_APP_ID=clxxxxxx  # 프론트엔드용 (NEXT_PUBLIC_ 접두사)
```

---

## 7. 참고 링크

- 공식 문서: https://docs.privy.io
- Solana 서명: https://docs.privy.io/wallets/using-wallets/solana/sign-a-message
- 대시보드: https://dashboard.privy.io
- 가격: https://privy.io/pricing (MAU 기반, 1,000명까지 무료)
