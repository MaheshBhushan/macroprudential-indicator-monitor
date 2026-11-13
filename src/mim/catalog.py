"""Series catalog: which public series are ingested and how they are identified."""

from __future__ import annotations

from dataclasses import dataclass

BIS_COUNTRIES = ("US", "GB", "DE", "FR", "ES", "IT", "JP", "CA")
EUROSTAT_COUNTRIES = ("DE", "FR", "ES", "IT")


@dataclass(frozen=True)
class Series:
    source: str  # BIS | ECB | ESTAT
    dataflow: str
    key: str
    country: str
    indicator: str
    unit: str
    expected_freq: str  # A | Q | M
    description: str

    @property
    def series_id(self) -> str:
        return f"{self.source}.{self.dataflow}.{self.key}"


# indicator -> (dataflow, key template, unit, description)
_BIS = {
    "credit_gap": (
        "WS_CREDIT_GAP",
        "Q.{c}.P.A.C",
        "pp of GDP",
        "Credit-to-GDP gap (actual minus HP-filtered trend), private non-financial sector",
    ),
    "total_credit_gdp": (
        "WS_TC",
        "Q.{c}.P.A.M.770.A",
        "% of GDP",
        "Total credit to private non-financial sector, all lenders, % of GDP, break-adjusted",
    ),
    "rpp_real_index": (
        "WS_SPP",
        "Q.{c}.R.628",
        "index 2010=100",
        "Residential property prices, real, index 2010=100",
    ),
    "rpp_nominal_index": (
        "WS_SPP",
        "Q.{c}.N.628",
        "index 2010=100",
        "Residential property prices, nominal, index 2010=100",
    ),
    "dsr_private": (
        "WS_DSR",
        "Q.{c}.P",
        "% of income",
        "Debt service ratio, private non-financial sector",
    ),
    "dsr_households": (
        "WS_DSR",
        "Q.{c}.H",
        "% of income",
        "Debt service ratio, households and NPISHs",
    ),
    "dsr_nfc": (
        "WS_DSR",
        "Q.{c}.N",
        "% of income",
        "Debt service ratio, non-financial corporations",
    ),
}


def default_catalog() -> list[Series]:
    out: list[Series] = []
    for indicator, (flow, tmpl, unit, desc) in _BIS.items():
        for c in BIS_COUNTRIES:
            out.append(Series("BIS", flow, tmpl.format(c=c), c, indicator, unit, "Q", desc))
    for c in EUROSTAT_COUNTRIES:
        out.append(
            Series(
                "ESTAT",
                "gov_10q_ggdebt",
                f"Q.GD.S13.PC_GDP.{c}",
                c,
                "gov_debt_gdp",
                "% of GDP",
                "Q",
                "General government gross debt (Maastricht), % of GDP",
            )
        )
    out.append(
        Series(
            "ECB",
            "BSI",
            "M.U2.Y.U.A20.A.I.U2.2250.Z01.A",
            "U2",
            "hh_loan_growth",
            "% y/y",
            "M",
            "MFI loans to euro area households, annual growth rate",
        )
    )
    out.append(
        Series(
            "ECB",
            "BSI",
            "M.U2.Y.U.A20.A.I.U2.2240.Z01.A",
            "U2",
            "nfc_loan_growth",
            "% y/y",
            "M",
            "MFI loans to euro area non-financial corporations, annual growth rate",
        )
    )
    return out
