"""
4Kカウンター データ取得スクリプト
J-Quants API (https://jpx-jquants.com/) から日本上場企業の財務データを取得し、
4K基準（高成長・高収益・高財務）を評価して data.json を生成します。

使用前に J-Quants アカウント（無料登録可）を作成してください。
環境変数 JQUANTS_EMAIL と JQUANTS_PASSWORD を設定してください。

  export JQUANTS_EMAIL=your@email.com
  export JQUANTS_PASSWORD=yourpassword
  python fetch_data.py
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ===== CONFIG =====
JQUANTS_EMAIL    = os.environ.get("JQUANTS_EMAIL", "")
JQUANTS_PASSWORD = os.environ.get("JQUANTS_PASSWORD", "")
OUTPUT_FILE      = "data.json"
API_BASE         = "https://api.jquants.com/v1"
REQUEST_DELAY    = 0.3   # seconds between requests (rate limit consideration)
MAX_RETRIES      = 3

# 4K 基準
CAGR_THRESHOLD          = 0.10   # 高成長: 3年CAGR >= 10%
OPM_THRESHOLD           = 0.10   # 高収益: 営業利益率 > 10%
EQUITY_RATIO_GENERAL    = 0.50   # 高財務: 一般企業 >= 50%
EQUITY_RATIO_FINANCIAL  = 0.10   # 高財務: 金融・不動産 >= 10%

FINANCIAL_SECTORS = {
    "銀行業", "証券・先物商品取引業", "保険業", "その他金融業", "不動産業"
}

# ===== AUTH =====
class JQuantsClient:
    def __init__(self, email: str, password: str):
        self.email    = email
        self.password = password
        self.id_token: Optional[str] = None

    def authenticate(self):
        log.info("J-Quants 認証中...")
        # Step 1: refresh token
        r = self._post("/token/auth_user",
                       json={"mailaddress": self.email, "password": self.password},
                       auth=False)
        refresh_token = r["refreshToken"]

        # Step 2: ID token
        r2 = self._post("/token/auth_refresh",
                        params={"refreshtoken": refresh_token}, auth=False)
        self.id_token = r2["idToken"]
        log.info("認証完了")

    def _headers(self):
        return {"Authorization": f"Bearer {self.id_token}"} if self.id_token else {}

    def _post(self, path, json=None, params=None, auth=True):
        url = API_BASE + path
        for attempt in range(MAX_RETRIES):
            try:
                r = requests.post(url, json=json, params=params,
                                  headers=self._headers() if auth else {},
                                  timeout=30)
                r.raise_for_status()
                return r.json()
            except requests.HTTPError as e:
                if e.response.status_code == 429:
                    log.warning(f"Rate limit hit, waiting 60s...")
                    time.sleep(60)
                elif attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise

    def _get(self, path, params=None):
        url = API_BASE + path
        for attempt in range(MAX_RETRIES):
            try:
                r = requests.get(url, params=params, headers=self._headers(), timeout=30)
                r.raise_for_status()
                return r.json()
            except requests.HTTPError as e:
                if e.response.status_code == 429:
                    log.warning(f"Rate limit hit, waiting 60s...")
                    time.sleep(60)
                elif attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise

    def get_listed_info(self) -> list:
        """全上場銘柄一覧を取得"""
        log.info("上場銘柄一覧を取得中...")
        data = self._get("/listed/info")
        return data.get("info", [])

    def get_financial_statements(self, code: str) -> list:
        """銘柄の財務諸表を取得（全期間）"""
        data = self._get("/fins/statements", params={"code": code})
        return data.get("statements", [])

# ===== SCORE CALCULATION =====
def is_financial(sector: str) -> bool:
    return sector in FINANCIAL_SECTORS

def safe_float(val) -> Optional[float]:
    try:
        f = float(val)
        return f if f != 0 else None
    except (TypeError, ValueError):
        return None

def calc_cagr_3yr(statements: list) -> Optional[float]:
    """直近3年間の売上高CAGRを計算"""
    # 年次決算のみ抽出（TypeOfCurrentPeriod == 'FY' or similar）
    annual = []
    for s in statements:
        tp = s.get("TypeOfCurrentPeriod", "")
        if tp in ("FY", "Annual", "通期"):
            end_date = s.get("CurrentFiscalYearEndDate", "")
            net_sales = safe_float(s.get("NetSales"))
            if end_date and net_sales and net_sales > 0:
                annual.append({"end_date": end_date, "net_sales": net_sales})

    if not annual:
        # TypeOfCurrentPeriod が設定されていない場合は全件から推定
        for s in statements:
            net_sales = safe_float(s.get("NetSales"))
            end_date  = s.get("CurrentFiscalYearEndDate", "")
            if end_date and net_sales and net_sales > 0:
                annual.append({"end_date": end_date, "net_sales": net_sales})

    if len(annual) < 4:
        return None

    # 日付でソート
    annual.sort(key=lambda x: x["end_date"])
    latest    = annual[-1]["net_sales"]
    three_ago = annual[-4]["net_sales"]

    if three_ago <= 0:
        return None

    cagr = (latest / three_ago) ** (1 / 3) - 1
    return round(cagr, 4)

def calc_op_margins(statements: list) -> tuple[Optional[float], Optional[float]]:
    """
    今期・来期の営業利益率を計算
    Returns: (current_margin, next_margin)
    """
    if not statements:
        return None, None

    # 最新の開示を取得
    stmts_sorted = sorted(statements,
                          key=lambda x: (x.get("DisclosedDate",""), x.get("DisclosedTime","")),
                          reverse=True)
    latest = stmts_sorted[0]

    # 今期営業利益率
    # 実績値があればそれを使用、なければ予想値
    op_profit  = safe_float(latest.get("OperatingProfit"))
    net_sales  = safe_float(latest.get("NetSales"))

    # 予想値（今期）
    f_op    = safe_float(latest.get("ForecastOperatingProfit"))
    f_sales = safe_float(latest.get("ForecastNetSales"))

    # 来期予想値
    ny_op    = safe_float(latest.get("NextYearForecastOperatingProfit"))
    ny_sales = safe_float(latest.get("NextYearForecastNetSales"))

    # 今期OPM計算
    current_margin = None
    if f_op is not None and f_sales and f_sales > 0:
        current_margin = round(f_op / f_sales, 4)
    elif op_profit is not None and net_sales and net_sales > 0:
        current_margin = round(op_profit / net_sales, 4)

    # 来期OPM計算
    next_margin = None
    if ny_op is not None and ny_sales and ny_sales > 0:
        next_margin = round(ny_op / ny_sales, 4)

    return current_margin, next_margin

def calc_equity_ratio(statements: list) -> Optional[float]:
    """最新の自己資本比率を取得"""
    if not statements:
        return None

    stmts_sorted = sorted(statements,
                          key=lambda x: (x.get("DisclosedDate",""), x.get("DisclosedTime","")),
                          reverse=True)

    for s in stmts_sorted:
        # J-Quants は EquityToAssetRatio を直接提供（0〜1の小数 or パーセント表記）
        er = safe_float(s.get("EquityToAssetRatio"))
        if er is not None:
            # パーセント表記（0〜100）の場合は変換
            if er > 1.5:
                er = er / 100
            return round(er, 4)

        # 直接計算
        equity      = safe_float(s.get("Equity"))
        total_assets = safe_float(s.get("TotalAssets"))
        if equity is not None and total_assets and total_assets > 0:
            return round(equity / total_assets, 4)

    return None

def calc_score(cagr, opm_c, opm_n, er, sector):
    financial = is_financial(sector)
    threshold = EQUITY_RATIO_FINANCIAL if financial else EQUITY_RATIO_GENERAL

    high_growth  = cagr  is not None and cagr  >= CAGR_THRESHOLD
    high_profit  = (opm_c is not None and opm_c > OPM_THRESHOLD
                 and opm_n is not None and opm_n > OPM_THRESHOLD)
    high_finance = er    is not None and er    >= threshold

    score = (34 if high_growth else 0) + (33 if high_profit else 0) + (33 if high_finance else 0)
    return high_growth, high_profit, high_finance, score

def get_fiscal_year_end(statements: list) -> str:
    if not statements:
        return ""
    stmts_sorted = sorted(statements,
                          key=lambda x: x.get("DisclosedDate",""), reverse=True)
    end_date = stmts_sorted[0].get("CurrentFiscalYearEndDate", "")
    if end_date:
        return end_date[:7]  # YYYY-MM
    return ""

# ===== MAIN =====
def main():
    if not JQUANTS_EMAIL or not JQUANTS_PASSWORD:
        log.error("環境変数 JQUANTS_EMAIL と JQUANTS_PASSWORD を設定してください")
        sys.exit(1)

    client = JQuantsClient(JQUANTS_EMAIL, JQUANTS_PASSWORD)
    client.authenticate()

    # 上場企業一覧
    listed = client.get_listed_info()
    log.info(f"上場企業数: {len(listed)}")

    results = []
    errors  = []

    for i, company in enumerate(listed):
        code   = company.get("Code", "")
        name   = company.get("CompanyName", "")
        sector = company.get("Sector33CodeName", "")
        market = company.get("MarketCodeName", "")

        if not code:
            continue

        try:
            time.sleep(REQUEST_DELAY)
            stmts = client.get_financial_statements(code)

            cagr         = calc_cagr_3yr(stmts)
            opm_c, opm_n = calc_op_margins(stmts)
            er           = calc_equity_ratio(stmts)
            fy_end       = get_fiscal_year_end(stmts)
            financial    = is_financial(sector)
            hg, hp, hf, score = calc_score(cagr, opm_c, opm_n, er, sector)

            results.append({
                "code":               code,
                "name":               name,
                "sector":             sector,
                "market":             market,
                "is_financial_sector": financial,
                "cagr_3yr":           cagr,
                "op_margin_current":  opm_c,
                "op_margin_next":     opm_n,
                "equity_ratio":       er,
                "fiscal_year_end":    fy_end,
            })

            if (i + 1) % 100 == 0:
                log.info(f"進捗: {i+1}/{len(listed)} 件処理完了")

        except Exception as e:
            log.warning(f"{code} {name}: エラー — {e}")
            errors.append({"code": code, "name": name, "error": str(e)})

    # スコア降順でソート
    results.sort(key=lambda x: (
        -(34 if (x["cagr_3yr"] or 0) >= CAGR_THRESHOLD else 0)
        -(33 if ((x["op_margin_current"] or 0) > OPM_THRESHOLD and (x["op_margin_next"] or 0) > OPM_THRESHOLD) else 0)
        -(33 if (x["equity_ratio"] or 0) >= (EQUITY_RATIO_FINANCIAL if x["is_financial_sector"] else EQUITY_RATIO_GENERAL) else 0)
    ))

    output = {
        "updated_at":  datetime.now().isoformat(timespec="seconds"),
        "is_sample":   False,
        "total":       len(results),
        "errors":      len(errors),
        "companies":   results,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log.info(f"完了: {len(results)} 社のデータを {OUTPUT_FILE} に保存しました")
    if errors:
        log.warning(f"エラー: {len(errors)} 社でデータ取得に失敗しました")

if __name__ == "__main__":
    main()
