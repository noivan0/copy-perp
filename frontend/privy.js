/**
 * Privy 지갑 연동 모듈
 * https://www.privy.io/
 *
 * 실제 운영: @privy-io/react-auth npm 패키지 사용
 * 현재: Vanilla JS stub (React 전환 전 인터페이스 정의)
 *
 * Privy 설정 필요:
 *   PRIVY_APP_ID = 환경변수
 *   지원: Google, Twitter, Discord, Email, Wallet
 *   임베디드 지갑: 소셜 로그인 후 자동 Solana 지갑 생성
 */

const PRIVY_APP_ID = window.PRIVY_APP_ID || 'YOUR_PRIVY_APP_ID';

// ── Privy 상태 ──────────────────────────────────
let privyUser = null;
let privyWallet = null;

/**
 * Privy 초기화
 * React 환경에서는 PrivyProvider로 교체
 */
async function initPrivy() {
  // TODO: React 전환 시 아래로 교체
  // import { PrivyProvider } from '@privy-io/react-auth';
  // <PrivyProvider appId={PRIVY_APP_ID} config={{ embeddedWallets: { createOnLogin: 'users-without-wallets' }}}>

  console.log('[Privy] 초기화 (stub mode)');
  return { ready: true, authenticated: false };
}

/**
 * 소셜 로그인
 * @param {'google'|'twitter'|'discord'|'email'} provider
 */
async function loginWithPrivy(provider = 'google') {
  // stub: 실제 구현 시 privy.login() 호출
  // const { login } = usePrivy();
  // await login();

  console.log(`[Privy] ${provider} 로그인 시도`);

  // Mock: 로그인 시뮬레이션
  privyUser = {
    id: 'privy:mock-user-123',
    email: 'user@example.com',
    linkedAccounts: [{ type: provider }],
  };

  // Mock 임베디드 지갑 (Solana)
  privyWallet = {
    address: '3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ',
    chainType: 'solana',
    walletClientType: 'privy',
  };

  return { user: privyUser, wallet: privyWallet };
}

/**
 * Builder Code 승인 서명 요청
 * Privy 임베디드 지갑으로 서명 → 서버에 전달
 *
 * 실제 구현:
 *   const { signMessage } = usePrivy();
 *   const signature = await signMessage(message);
 */
async function signBuilderCodeApproval(builderCode = 'copyperp', maxFeeRate = '0.0005') {
  if (!privyWallet) throw new Error('지갑 미연결');

  const payload = {
    timestamp: Date.now(),
    expiry_window: 5000,
    type: 'approve_builder_code',
    data: { builder_code: builderCode, max_fee_rate: maxFeeRate },
  };

  // TODO: Privy signMessage로 교체
  // const message = JSON.stringify(sortKeys(payload));
  // const { signature } = await privy.signMessage({ message });

  console.log('[Privy] Builder Code 승인 서명 요청:', payload);

  // stub: 서버로 전달
  const response = await fetch('/api/v1/account/builder_codes/approve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      account: privyWallet.address,
      signature: 'STUB_SIGNATURE',  // 실제: Privy 서명값
      ...payload,
    }),
  });

  return response.ok;
}

/**
 * 지갑 주소 반환
 */
function getWalletAddress() {
  return privyWallet?.address || null;
}

/**
 * 로그아웃
 */
async function logoutPrivy() {
  privyUser = null;
  privyWallet = null;
  console.log('[Privy] 로그아웃');
}

// ── React 컴포넌트 (참조용) ──────────────────────────────────
/**
 * React + Privy 실제 구현 예시 (Next.js 14 App Router):
 *
 * // app/providers.tsx
 * 'use client';
 * import { PrivyProvider } from '@privy-io/react-auth';
 * export function Providers({ children }) {
 *   return (
 *     <PrivyProvider
 *       appId={process.env.NEXT_PUBLIC_PRIVY_APP_ID}
 *       config={{
 *         loginMethods: ['google', 'twitter', 'email', 'wallet'],
 *         appearance: { theme: 'dark' },
 *         embeddedWallets: {
 *           createOnLogin: 'users-without-wallets',
 *           noPromptOnSignature: false,
 *         },
 *         defaultChain: { id: 'solana:mainnet' },
 *       }}
 *     >
 *       {children}
 *     </PrivyProvider>
 *   );
 * }
 *
 * // components/ConnectButton.tsx
 * import { usePrivy } from '@privy-io/react-auth';
 * export function ConnectButton() {
 *   const { ready, authenticated, login, logout, user } = usePrivy();
 *   const wallet = user?.linkedAccounts?.find(a => a.type === 'wallet');
 *   if (!ready) return <button disabled>Loading...</button>;
 *   if (!authenticated) return <button onClick={login}>Connect</button>;
 *   return (
 *     <div>
 *       <span>{wallet?.address?.slice(0,8)}...</span>
 *       <button onClick={logout}>Disconnect</button>
 *     </div>
 *   );
 * }
 */

export { initPrivy, loginWithPrivy, signBuilderCodeApproval, getWalletAddress, logoutPrivy };
