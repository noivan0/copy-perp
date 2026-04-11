"""
AutoResearch 자율 루프 — Karpathy autoresearch 방법론 적용

루프:
  while True:
    1. 외부 리소스 수집 (arXiv, GitHub, 논문) → 새 지표 아이디어
    2. scorer.py 수정 (가중치 / 새 지표 실험)
    3. evaluate.py 실행
    4. 개선 시 → git commit + 결과 기록
    5. 악화 시 → git checkout scorer.py (revert)
    6. 30분마다 현황 보고

실행:
  python3 autoresearch/run_loop.py [--max-experiments 50]
"""
import subprocess, json, time, os, sys, random, math, re
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

BASE_DIR   = Path(__file__).parent.parent
SCORER_F   = BASE_DIR / "autoresearch" / "scorer.py"
RESULT_F   = BASE_DIR / "autoresearch" / "results.jsonl"
IDEAS_F    = BASE_DIR / "autoresearch" / "ideas.jsonl"
REPORT_F   = BASE_DIR / "autoresearch" / "report.md"


# ── 외부 리소스 수집 ────────────────────────────────────

RESEARCH_TOPICS = [
    # arXiv
    "copy trading performance persistence alpha decay",
    "trader selection criteria risk adjusted returns",
    "momentum factor crypto performance",
    "drawdown based risk filtering portfolio",
    # 실전 지표
    "Ulcer Index performance metric portfolio",
    "Kelly criterion fractional position sizing",
    "Common Sense Ratio Van Tharp trading",
    "profit factor expectancy copy trading follower",
]

def fetch_arxiv_ideas(query: str, max_results: int = 3) -> list[dict]:
    """arXiv에서 관련 논문 제목/요약 수집"""
    import urllib.request, urllib.parse, xml.etree.ElementTree as ET
    q = urllib.parse.quote(query)
    url = f"https://export.arxiv.org/api/query?search_query=all:{q}&max_results={max_results}&sortBy=relevance"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            xml_data = r.read().decode("utf-8")
        root = ET.fromstring(xml_data)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        ideas = []
        for entry in root.findall("atom:entry", ns):
            title   = entry.find("atom:title", ns).text.strip()
            summary = entry.find("atom:summary", ns).text.strip()[:300]
            ideas.append({"source": "arxiv", "title": title, "summary": summary})
        return ideas
    except Exception as e:
        return []


def collect_research_ideas() -> list[dict]:
    """다양한 소스에서 지표 아이디어 수집"""
    ideas = []

    # arXiv 논문
    topic = random.choice(RESEARCH_TOPICS)
    arxiv_ideas = fetch_arxiv_ideas(topic)
    ideas.extend(arxiv_ideas)

    # 내장된 지표 아이디어 풀 (quantstats, 실전 copy trading 플랫폼 분석 기반)
    builtin_ideas = [
        {
            "source": "quantstats",
            "title": "Omega Ratio — 목표수익률 기준 상/하방 분리",
            "metric": "omega_ratio",
            "formula": "sum(max(R-L,0)) / sum(max(L-R,0))",
            "w_key": "w_omega",
            "note": "L=0 기준, PF의 연속형 버전. copy trading에 적합",
        },
        {
            "source": "etoro_research",
            "title": "Copier Profitability Score — 실제 팔로워 수익 기반",
            "metric": "copier_pnl_ratio",
            "formula": "avg_follower_pnl / avg_leader_pnl",
            "w_key": "w_copier",
            "note": "eToro 내부 지표. 팔로워 비용 차감 후 실질 수익률",
        },
        {
            "source": "zulutrade",
            "title": "Drawdown Recovery Speed — MDD 회복 속도",
            "metric": "recovery_speed",
            "formula": "1 / (mdd_duration_days + 1)",
            "w_key": "w_recovery_speed",
            "note": "ZuluTrade 핵심 지표. 오래 묻힌 포지션 페널티",
        },
        {
            "source": "bitget_copy",
            "title": "Trade Frequency Filter — 팔로워 슬리피지 추정",
            "metric": "freq_adjusted_ept",
            "formula": "ept_gross - (trades_per_day * avg_slippage_pct * avg_pos)",
            "w_key": "w_freq_adj",
            "note": "Bitget Copy Trading 필터. 고빈도일수록 팔로워 비용 증가",
        },
        {
            "source": "academic",
            "title": "Information Ratio — 벤치마크 대비 초과수익 일관성",
            "metric": "information_ratio",
            "formula": "annualized(excess_return) / tracking_error",
            "w_key": "w_ir",
            "note": "Grinold & Kahn 1999. 시장 대비 알파 일관성 측정",
        },
        {
            "source": "risk_mgmt",
            "title": "Tail Ratio — 상/하방 꼬리 비대칭",
            "metric": "tail_ratio",
            "formula": "P95_return / abs(P5_return)",
            "w_key": "w_tail",
            "note": "Van Tharp CSR 구성요소. 1.5 이상이면 우상향 편향",
        },
        {
            "source": "crypto_specific",
            "title": "Funding Rate Sensitivity — 펀딩비 의존도",
            "metric": "funding_sensitivity",
            "formula": "1 - (open_pnl_ratio / total_pnl)",
            "w_key": "w_funding",
            "note": "롱온리 + 미청산 비율 높으면 펀딩비 수익자 의심",
        },
    ]
    ideas.extend(random.sample(builtin_ideas, min(3, len(builtin_ideas))))

    # 기록
    with open(IDEAS_F, "a") as f:
        for idea in ideas:
            f.write(json.dumps({**idea, "collected_at": datetime.now().isoformat()}) + "\n")

    return ideas


# ── scorer.py 수정 ─────────────────────────────────────

def mutate_scorer(ideas: list[dict], experiment_n: int) -> str:
    """
    아이디어 기반으로 scorer.py 가중치 조정.
    strategy:
      - 짝수 실험: 무작위 가중치 ±20% perturbation
      - 홀수 실험: 아이디어에서 파생된 방향성 조정
    """
    with open(SCORER_F) as f:
        code = f.read()

    changes = []

    if experiment_n % 3 == 0:
        # 전략 A: EPT_net 가중치 강화 (팔로워 수익 직결)
        code = _set_weight(code, "w_ept_net",      min(0.35, _get_weight(code, "w_ept_net") + 0.05))
        code = _set_weight(code, "w_profit_factor", max(0.10, _get_weight(code, "w_profit_factor") - 0.05))
        changes.append("EPT_net 가중치 +5%, PF -5%")

    elif experiment_n % 3 == 1:
        # 전략 B: Purity 강화 (펀딩비 필터)
        code = _set_weight(code, "w_purity",   min(0.15, _get_weight(code, "w_purity") + 0.03))
        code = _set_weight(code, "w_sample",   max(0.02, _get_weight(code, "w_sample") - 0.01))
        code = _set_weight(code, "w_freq_penalty", 0.02)
        changes.append("Purity +3%, 고빈도 페널티 활성화")

    else:
        # 전략 C: 무작위 perturbation (탐색)
        weights = ["w_profit_factor", "w_sharpe", "w_ept_net", "w_sortino",
                   "w_mdd", "w_purity", "w_sample"]
        w1, w2 = random.sample(weights, 2)
        delta = round(random.uniform(0.02, 0.06), 2)
        v1 = _get_weight(code, w1)
        v2 = _get_weight(code, w2)
        if v1 + delta <= 0.40 and v2 - delta >= 0.01:
            code = _set_weight(code, w1, v1 + delta)
            code = _set_weight(code, w2, v2 - delta)
            changes.append(f"{w1} +{delta}, {w2} -{delta}")
        else:
            changes.append("perturbation 범위 초과 → 스킵")

    # 임계값 실험 (5회마다)
    if experiment_n % 5 == 0:
        cur = _get_threshold(code, "threshold_min_purity")
        new = round(cur + random.choice([-0.03, 0.03]), 2)
        new = max(0.15, min(0.40, new))
        code = _set_threshold(code, "threshold_min_purity", new)
        changes.append(f"purity 임계값 {cur} → {new}")

    with open(SCORER_F, "w") as f:
        f.write(code)

    return ", ".join(changes) if changes else "변경 없음"


def _get_weight(code: str, name: str) -> float:
    m = re.search(rf"^{re.escape(name)}\s*=\s*([\d.]+)", code, re.MULTILINE)
    return float(m.group(1)) if m else 0.1


def _set_weight(code: str, name: str, val: float) -> str:
    return re.sub(rf"^({re.escape(name)}\s*=\s*)[\d.]+",
                  lambda m: f"{m.group(1)}{val:.2f}", code, flags=re.MULTILINE)


def _get_threshold(code: str, name: str) -> float:
    m = re.search(rf"^{re.escape(name)}\s*=\s*([\d.]+)", code, re.MULTILINE)
    return float(m.group(1)) if m else 0.25


def _set_threshold(code: str, name: str, val: float) -> str:
    return re.sub(rf"^({re.escape(name)}\s*=\s*)[\d.]+",
                  lambda m: f"{m.group(1)}{val:.2f}", code, flags=re.MULTILINE)


def git_commit(msg: str):
    subprocess.run(["git", "add", "autoresearch/scorer.py", "autoresearch/results.jsonl",
                    "autoresearch/ideas.jsonl"],
                   cwd=BASE_DIR, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"[autoresearch] {msg}"],
                   cwd=BASE_DIR, capture_output=True)


def git_revert_scorer():
    subprocess.run(["git", "checkout", "HEAD", "--", "autoresearch/scorer.py"],
                   cwd=BASE_DIR, capture_output=True)


# ── 보고서 생성 ─────────────────────────────────────────

def generate_report(results: list[dict]):
    if not results:
        return
    best = min(results, key=lambda x: x["follower_loss"])
    lines = [
        f"# AutoResearch 진행 보고",
        f"**{datetime.now().strftime('%Y-%m-%d %H:%M')}**  |  총 실험: {len(results)}회\n",
        f"## 최고 결과",
        f"- follower_loss: **{best['follower_loss']:.6f}**",
        f"- 선별 트레이더: {best['n_traders']}명",
        f"- 가중 EPT_net: ${best['ept_net']:.4f}",
        f"- 가중치: EPT={best['weights'].get('w_ept_net')}, PF={best['weights'].get('w_profit_factor')}, Purity={best['weights'].get('w_purity')}",
        f"\n## 실험 히스토리",
    ]
    for r in results[-20:]:
        mark = "✅" if r.get("improved") else "❌"
        lines.append(
            f"{mark} {r['timestamp'][:16]}  loss={r['follower_loss']:.4f}  "
            f"n={r['n_traders']}  EPT=${r['ept_net']:.4f}  label={r.get('label','')}"
        )
    with open(REPORT_F, "w") as f:
        f.write("\n".join(lines))


# ── 메인 루프 ───────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-experiments", type=int, default=50)
    parser.add_argument("--eval-duration",   type=int, default=10)
    args = parser.parse_args()

    print(f"\n{'='*55}")
    print(f"🔬 AutoResearch 시작  |  max={args.max_experiments}회")
    print(f"{'='*55}\n")

    results      = []
    best_loss    = float("inf")
    no_improve   = 0
    last_report  = time.time()

    for exp_n in range(args.max_experiments):
        print(f"\n── 실험 #{exp_n+1}/{args.max_experiments} ──────────────────────")

        # 1. 외부 리소스 수집
        print("📚 리서치 아이디어 수집...")
        ideas = collect_research_ideas()
        print(f"   {len(ideas)}개 아이디어 수집")

        # 2. scorer.py 수정 (수정 전 백업 — 크래시 안전성 보장)
        print("✏️  scorer.py 수정...")
        _scorer_backup = None
        try:
            with open(SCORER_F) as _bf:
                _scorer_backup = _bf.read()
        except Exception as _bke:
            print(f"   [경고] scorer.py 백업 실패: {_bke}")
        change_desc = mutate_scorer(ideas, exp_n)
        print(f"   변경: {change_desc}")

        # 3. evaluate 실행
        eval_crashed = False
        try:
            result = subprocess.run(
                [sys.executable, "autoresearch/evaluate.py",
                 "--duration", str(args.eval_duration),
                 "--label", f"exp_{exp_n+1}_{change_desc[:30]}"],
                cwd=BASE_DIR,
                capture_output=True, text=True,
                timeout=300,  # 5분 타임아웃 (무한 대기 방지)
            )
            print(result.stdout[-800:] if result.stdout else "출력 없음")
        except subprocess.TimeoutExpired:
            print(f"[경고] evaluate.py 타임아웃 → scorer.py 원본 복원")
            eval_crashed = True
        except Exception as _eval_e:
            print(f"[경고] evaluate.py 실행 오류: {_eval_e} → scorer.py 원본 복원")
            eval_crashed = True

        # 크래시 시 원본 복원 (P1 Fix Round 5: rollback 보장)
        if eval_crashed:
            if _scorer_backup is not None:
                try:
                    with open(SCORER_F, "w") as _rf:
                        _rf.write(_scorer_backup)
                    print("   ✅ scorer.py 메모리 백업에서 복원 완료")
                except Exception as _re:
                    print(f"   ⚠️  메모리 복원 실패, git checkout 시도: {_re}")
                    git_revert_scorer()
            else:
                git_revert_scorer()
            no_improve += 1
            continue

        # 결과 파싱
        try:
            with open(RESULT_F) as f:
                lines = f.read().strip().split("\n")
            latest = json.loads(lines[-1])
            results.append(latest)
            loss = latest["follower_loss"]

            if latest.get("improved"):
                print(f"✅ 개선! loss={loss:.6f} (이전 {best_loss:.6f})")
                git_commit(f"exp#{exp_n+1} loss={loss:.6f} | {change_desc[:50]}")
                best_loss   = loss
                no_improve  = 0
            else:
                print(f"❌ 미개선. revert.")
                git_revert_scorer()
                no_improve += 1

        except Exception as e:
            print(f"결과 파싱 실패: {e}")
            # P1 Fix (Round 5): 결과 파싱 실패도 메모리 백업 우선 복원
            if _scorer_backup is not None:
                try:
                    with open(SCORER_F, "w") as _rf2:
                        _rf2.write(_scorer_backup)
                    print("   scorer.py 메모리 백업 복원 완료")
                except Exception:
                    git_revert_scorer()
            else:
                git_revert_scorer()

        # 3회 연속 미개선 → 더 공격적인 탐색
        if no_improve >= 3:
            print("⚡ 3회 연속 미개선 → 탐색 전략 변경")
            no_improve = 0

        # 30분마다 보고서
        if time.time() - last_report > 1800:
            generate_report(results)
            print(f"📊 보고서 업데이트: autoresearch/report.md")
            last_report = time.time()

        time.sleep(3)  # API rate limit 고려

    # 최종 보고서
    generate_report(results)
    print(f"\n{'='*55}")
    print(f"✅ AutoResearch 완료  |  총 {len(results)}회 실험")
    print(f"   최저 follower_loss: {min(r['follower_loss'] for r in results):.6f}")
    print(f"   보고서: autoresearch/report.md")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
