"""
투자자 동향 분석 모듈
외국인 / 기관 / 프로그램 / 개인(개미) 매매 심리 분석
"""
import yaml
from src.utils.logger import setup_logger

logger = setup_logger("investor")


class InvestorAnalyzer:
    def __init__(self):
        with open("config/strategy.yaml", "r", encoding="utf-8") as f:
            self._cfg = yaml.safe_load(f)["investor_analysis"]

    # ── 단일 종목 투자자 심리 분석 ────────────────────────────────
    def analyze_current(self, investor_data: dict) -> dict:
        """당일 투자자별 순매수 분석"""
        foreign = investor_data.get("foreign", 0)
        institution = investor_data.get("institution", 0)
        individual = investor_data.get("individual", 0)
        program = investor_data.get("program", 0)

        scores = {}
        # 외국인
        scores["foreign"] = self._score_investor(
            foreign,
            self._cfg["foreign"]["strong_buy_threshold"],
            contrarian=False,
        )
        # 기관
        scores["institution"] = self._score_investor(
            institution,
            self._cfg["institution"]["strong_buy_threshold"],
            contrarian=False,
        )
        # 프로그램
        scores["program"] = self._score_investor(
            program,
            self._cfg["institution"]["strong_buy_threshold"] // 2,
            contrarian=False,
        )
        # 개인 (역방향 지표)
        if self._cfg["individual"]["contrarian"]:
            scores["individual"] = self._score_investor(
                individual,
                self._cfg["individual"]["contrarian_threshold"],
                contrarian=True,
            )
        else:
            scores["individual"] = self._score_investor(individual, 100000, False)

        cfg = self._cfg
        weighted = (
            scores["foreign"] * cfg["foreign"]["weight"]
            + scores["institution"] * cfg["institution"]["weight"]
            + scores["program"] * cfg["program"]["weight"]
            + scores["individual"] * cfg["individual"]["weight"]
        )

        return {
            "score": round(max(-1.0, min(1.0, weighted)), 4),
            "scores": {k: round(v, 3) for k, v in scores.items()},
            "raw": investor_data,
            "summary": self._summarize(foreign, institution, individual, program),
        }

    def _score_investor(self, net_qty: int, strong_threshold: int, contrarian: bool) -> float:
        if net_qty > strong_threshold:
            base = 1.0
        elif net_qty > strong_threshold * 0.5:
            base = 0.6
        elif net_qty > 0:
            base = 0.3
        elif net_qty > -strong_threshold * 0.5:
            base = -0.3
        elif net_qty > -strong_threshold:
            base = -0.6
        else:
            base = -1.0
        return -base if contrarian else base

    def _summarize(self, foreign: int, institution: int, individual: int, program: int) -> str:
        parts = []
        if foreign > 0:
            parts.append(f"외국인 순매수 {foreign:,}주")
        elif foreign < 0:
            parts.append(f"외국인 순매도 {abs(foreign):,}주")
        if institution > 0:
            parts.append(f"기관 순매수 {institution:,}주")
        elif institution < 0:
            parts.append(f"기관 순매도 {abs(institution):,}주")
        if program > 0:
            parts.append(f"프로그램 순매수 {program:,}주")
        if individual > 50000:
            parts.append(f"개인 대량매수 {individual:,}주 (주의)")
        return " / ".join(parts) if parts else "뚜렷한 수급 없음"

    # ── 히스토리 기반 연속 매수/매도 분석 ───────────────────────
    def analyze_history(self, history: list[dict]) -> dict:
        """최근 N일 투자자 동향 추세 분석"""
        if not history:
            return {"score": 0.0, "trend": "데이터 없음"}

        recent = history[-5:]  # 최근 5일
        cfg_f = self._cfg["foreign"]
        cfg_i = self._cfg["institution"]

        foreign_streak = self._count_streak([d["foreign"] for d in recent])
        inst_streak = self._count_streak([d["institution"] for d in recent])

        # 연속 순매수 일수 기반 신호
        foreign_score = self._streak_to_score(foreign_streak, cfg_f["buy_signal_days"])
        inst_score = self._streak_to_score(inst_streak, cfg_i["buy_signal_days"])

        # 최근 5일 누적 순매수량
        foreign_total = sum(d["foreign"] for d in recent)
        inst_total = sum(d["institution"] for d in recent)
        program_total = sum(d["program"] for d in recent)
        individual_total = sum(d["individual"] for d in recent)

        # 개인 역방향 처리
        individual_score = self._score_investor(
            individual_total, self._cfg["individual"]["contrarian_threshold"] * 5,
            contrarian=self._cfg["individual"]["contrarian"],
        )

        weighted = (
            foreign_score * self._cfg["foreign"]["weight"]
            + inst_score * self._cfg["institution"]["weight"]
            + individual_score * self._cfg["individual"]["weight"]
        )

        trend_lines = []
        if foreign_streak > 0:
            trend_lines.append(f"외국인 {foreign_streak}일 연속 순매수")
        elif foreign_streak < 0:
            trend_lines.append(f"외국인 {abs(foreign_streak)}일 연속 순매도")
        if inst_streak > 0:
            trend_lines.append(f"기관 {inst_streak}일 연속 순매수")
        elif inst_streak < 0:
            trend_lines.append(f"기관 {abs(inst_streak)}일 연속 순매도")

        return {
            "score": round(max(-1.0, min(1.0, weighted)), 4),
            "foreign_streak": foreign_streak,
            "institution_streak": inst_streak,
            "5d_foreign_total": foreign_total,
            "5d_institution_total": inst_total,
            "5d_program_total": program_total,
            "5d_individual_total": individual_total,
            "trend": " / ".join(trend_lines) if trend_lines else "추세 없음",
        }

    def _count_streak(self, values: list[int]) -> int:
        """연속 양수(+)/음수(-) 일수 반환. 양수면 +N, 음수면 -N"""
        if not values:
            return 0
        streak = 0
        direction = 1 if values[-1] > 0 else -1
        for v in reversed(values):
            if direction == 1 and v > 0:
                streak += 1
            elif direction == -1 and v < 0:
                streak += 1
            else:
                break
        return streak * direction

    def _streak_to_score(self, streak: int, signal_days: int) -> float:
        if streak >= signal_days:
            return min(1.0, 0.4 + streak * 0.15)
        elif streak > 0:
            return streak * 0.2
        elif streak <= -signal_days:
            return max(-1.0, -(0.4 + abs(streak) * 0.15))
        elif streak < 0:
            return streak * 0.2
        return 0.0

    # ── 종합 투자자 심리 점수 ────────────────────────────────────
    def get_investor_score(self, current: dict, history: list[dict]) -> dict:
        """당일 + 히스토리 결합 최종 투자자 심리 점수"""
        current_result = self.analyze_current(current)
        history_result = self.analyze_history(history)

        # 당일 50% + 히스토리 50% 가중
        combined_score = current_result["score"] * 0.5 + history_result["score"] * 0.5

        logger.info(
            f"투자자 심리 점수: {combined_score:.3f} "
            f"(당일={current_result['score']:.3f}, 추세={history_result['score']:.3f})"
        )
        return {
            "score": round(combined_score, 4),
            "current": current_result,
            "history": history_result,
        }

    # ── 시장 전체 수급 분석 (종목 추천용) ───────────────────────
    def rank_stocks_by_investor(self, stocks_data: list[dict]) -> list[dict]:
        """
        여러 종목의 투자자 동향을 분석하여 점수 순으로 정렬
        stocks_data: [{"ticker": ..., "current": {...}, "history": [...]}]
        """
        ranked = []
        for stock in stocks_data:
            try:
                result = self.get_investor_score(
                    stock["current"], stock.get("history", [])
                )
                ranked.append({
                    "ticker": stock["ticker"],
                    "name": stock.get("name", ""),
                    "investor_score": result["score"],
                    "summary": result["current"]["summary"],
                    "trend": result["history"]["trend"],
                    "foreign_streak": result["history"]["foreign_streak"],
                    "inst_streak": result["history"]["institution_streak"],
                })
            except Exception as e:
                logger.error(f"투자자 분석 오류 {stock.get('ticker')}: {e}")

        return sorted(ranked, key=lambda x: x["investor_score"], reverse=True)
