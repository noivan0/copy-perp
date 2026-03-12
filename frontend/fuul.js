/**
 * Fuul 레퍼럴 SDK 연동 모듈
 * https://www.fuul.xyz/
 * npm: @fuul/sdk
 *
 * Fuul SVM (Solana) 지원 확인 ✅
 * 2026-01 보안 감사 완료
 *
 * 설치: npm install @fuul/sdk
 */

// ── Fuul 초기화 (stub) ──────────────────────────────────
// 실제 구현:
// import { Fuul, UserIdentifierType } from '@fuul/sdk';
// Fuul.initialize({ apiKey: process.env.NEXT_PUBLIC_FUUL_API_KEY });

const FUUL_API_KEY = window.FUUL_API_KEY || '';

/**
 * 레퍼럴 코드 목록 조회
 * @param {string} walletAddress Solana 지갑 주소
 */
async function listReferralCodes(walletAddress) {
  // 실제:
  // const result = await Fuul.listUserReferralCodes({
  //   user_identifier: walletAddress,
  //   user_identifier_type: UserIdentifierType.SolanaAddress,
  // });
  // return result.referral_codes;

  // stub: 서버 API 통해 조회
  try {
    const res = await fetch(`/followers/${walletAddress}/referral`);
    const json = await res.json();
    return [{ code: json.referral_link?.split('ref=')[1], link: json.referral_link }];
  } catch {
    return [];
  }
}

/**
 * 레퍼럴 상태 확인
 * @param {string} walletAddress
 */
async function checkReferralStatus(walletAddress) {
  // 실제:
  // return await Fuul.checkReferralStatus({
  //   user_identifier: walletAddress,
  //   user_identifier_type: UserIdentifierType.SolanaAddress,
  // });

  try {
    const res = await fetch(`/followers/${walletAddress}/referral`);
    return await res.json();
  } catch {
    return { points: 0, referral_link: null };
  }
}

/**
 * 포인트 리더보드
 */
async function getPointsLeaderboard(limit = 10) {
  // 실제:
  // return await Fuul.getLeaderboard({ limit });

  try {
    const res = await fetch(`/fuul/leaderboard?limit=${limit}`);
    const json = await res.json();
    return json.data || [];
  } catch {
    return [];
  }
}

/**
 * 레퍼럴 링크 공유 버튼 UI 생성
 */
function createShareButton(referralLink, containerId) {
  const container = document.getElementById(containerId);
  if (!container || !referralLink) return;

  const twitterText = encodeURIComponent(
    `I'm copy trading on @PacificaFi with Copy Perp!\nJoin me and earn rewards: ${referralLink}`
  );
  const twitterUrl = `https://twitter.com/intent/tweet?text=${twitterText}`;

  container.innerHTML = `
    <div class="flex gap-3 items-center">
      <input value="${referralLink}" readonly
        class="flex-1 bg-white/10 rounded px-3 py-2 text-sm text-gray-300 select-all"
        onclick="this.select()" />
      <button onclick="navigator.clipboard.writeText('${referralLink}').then(()=>alert('Copied!'))"
        class="bg-blue-600 hover:bg-blue-700 px-3 py-2 rounded text-sm transition">
        Copy
      </button>
      <a href="${twitterUrl}" target="_blank"
        class="bg-sky-500 hover:bg-sky-600 px-3 py-2 rounded text-sm transition">
        𝕏 Share
      </a>
    </div>
  `;
}

/**
 * React 컴포넌트 참조 (실제 구현용):
 *
 * // components/FuulReferral.tsx
 * import { Fuul, UserIdentifierType } from '@fuul/sdk';
 * import { usePrivy } from '@privy-io/react-auth';
 *
 * export function ReferralCard() {
 *   const { user } = usePrivy();
 *   const wallet = user?.linkedAccounts?.find(a => a.type === 'wallet')?.address;
 *   const [codes, setCodes] = useState([]);
 *
 *   useEffect(() => {
 *     if (!wallet) return;
 *     Fuul.listUserReferralCodes({
 *       user_identifier: wallet,
 *       user_identifier_type: UserIdentifierType.SolanaAddress,
 *     }).then(r => setCodes(r.referral_codes));
 *   }, [wallet]);
 *
 *   return (
 *     <div>
 *       {codes.map(code => (
 *         <div key={code.code}>
 *           <span>{code.link}</span>
 *           <button onClick={() => navigator.clipboard.writeText(code.link)}>Copy</button>
 *         </div>
 *       ))}
 *     </div>
 *   );
 * }
 */

export { listReferralCodes, checkReferralStatus, getPointsLeaderboard, createShareButton };
