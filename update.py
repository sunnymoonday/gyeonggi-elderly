# -*- coding: utf-8 -*-
"""경기도 65세 이상 노인인구 대시보드 데이터 갱신 스크립트.

매월 1일 실행:
  1) 행정안전부 주민등록 인구통계(jumin.mois.go.kr)에서 최신 월 연령별·성별 인구 CSV 수집
  2) 공공데이터포털 '보건복지부_기초연금인구수급율현황'(15161618) CSV 수집(실패 시 이전 파일 사용)
  3) data.json 저장 후 template.html 에 넣어 index.html 생성

사용법:  python update.py
"""
import csv
import datetime as dt
import io
import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request

BASE = pathlib.Path(__file__).resolve().parent
RAW = BASE / "raw"
RAW.mkdir(exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (data-refresh script)"}
MOIS = "https://jumin.mois.go.kr"
PT_DONGS = ["원평동", "세교동", "비전1동", "비전2동", "동삭동", "용이동", "신평동"]
AGE_GROUPS = [("65~69세", ["65~69세"]), ("70~74세", ["70~74세"]), ("75~79세", ["75~79세"]),
              ("80~84세", ["80~84세"]), ("85세 이상", ["85~89세", "90~94세", "95~99세", "100세 이상"])]


def http(url, data=None, timeout=120):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_mois_month():
    """통계 조회 화면의 기본 선택값 = 공개된 최신 월."""
    html = http(f"{MOIS}/ageStatMonth.do").decode("utf-8", "replace")
    y = re.search(r'value="(\d{4})" name="searchYearEnd"', html).group(1)
    m = re.search(r'value="(\d{2})" name="searchMonthEnd"', html).group(1)
    return y, m


def fetch_mois(y, m):
    form = {
        "sltOrgType": "1", "sltOrgLvl1": "4100000000", "sltOrgLvl2": "A", "gender": "gender",
        "sum": "sum", "sltUndefType": "", "searchYearStart": y, "searchMonthStart": m,
        "searchYearEnd": y, "searchMonthEnd": m, "sltOrderType": "1", "sltOrderValue": "ASC",
        "sltArgTypes": "5", "sltArgTypeA": "0", "sltArgTypeB": "100", "category": "month",
    }
    raw = http(f"{MOIS}/downloadCsvAge.do?searchYearMonth=month&xlsStats=3", form)
    if len(raw) < 10000:
        raise RuntimeError("주민등록 인구 CSV 수집 실패")
    (RAW / f"mois_{y}{m}.csv").write_bytes(raw)
    return raw.decode("cp949")


def num(s):
    return int(str(s).replace(",", "").strip() or 0)


def parse_mois(text):
    rows = list(csv.reader(io.StringIO(text)))
    head = rows[0]

    def col(sex, band):
        for i, h in enumerate(head):
            if h.endswith(f"_{sex}_{band}"):
                return i
        raise KeyError(f"{sex} {band}")

    idx = {sex: {g: [col(sex, b) for b in bands] for g, bands in AGE_GROUPS} for sex in ("남", "여")}
    tot = col("계", "총인구수")
    out = []
    for r in rows[1:]:
        m = re.match(r"\s*(.+?)\s*\((\d{10})\)", r[0])
        if not m or not m.group(2).startswith("41"):
            continue
        name, code = m.group(1).split(), m.group(2)
        rec = {"code": code, "name": name[-1], "total": num(r[tot]), "age": {}, "male": 0, "female": 0}
        for g, _ in AGE_GROUPS:
            mv = sum(num(r[i]) for i in idx["남"][g])
            fv = sum(num(r[i]) for i in idx["여"][g])
            rec["age"][g] = {"m": mv, "f": fv}
            rec["male"] += mv
            rec["female"] += fv
        rec["elderly"] = rec["male"] + rec["female"]
        rec["level"] = ("sido" if code == "4100000000" else
                        "sigun" if code.endswith("000000") else
                        "gu" if code.endswith("00000") else "dong")
        rec["parent"] = name[1] if len(name) > 1 else ""
        out.append(rec)
    return out


def fetch_pension():
    """기초연금 수급자 현황(시군구·월별). 실패하면 직전에 받아 둔 파일을 사용."""
    path = RAW / "pension.csv"
    try:
        meta = json.loads(http("https://www.data.go.kr/tcs/dss/selectFileDataDownload.do", {
            "publicDataDetailPk": "uddi:4ab07535-f2e7-4be5-a8e3-e3dba26de890",
            "publicDataPk": "15161618", "atchFileId": "", "fileDetailSn": "1",
            "publicDataTyCode": "PR0051"}))
        raw = http("https://www.data.go.kr/cmm/cmm/fileDownload.do?" + urllib.parse.urlencode(
            {"atchFileId": meta["atchFileId"], "fileDetailSn": meta["fileDetailSn"]}))
        if len(raw) > 1000:
            path.write_bytes(raw)
    except Exception as e:  # noqa: BLE001
        print("기초연금 파일 갱신 실패, 기존 파일 사용:", e, file=sys.stderr)
    rows = list(csv.reader(io.StringIO(path.read_bytes().decode("cp949"))))[1:]
    gg = [r for r in rows if r[1].startswith("경기도")]
    latest = max(r[0] for r in gg)
    data = {}
    for r in gg:
        if r[0] == latest:
            data[r[1].split()[1]] = {"pop65": num(r[2]), "recipients": num(r[3]), "rate": float(r[4])}
    return latest, data


def main():
    y, m = latest_mois_month()
    recs = parse_mois(fetch_mois(y, m))
    p_month, pension = fetch_pension()

    sido = next(r for r in recs if r["level"] == "sido")
    siguns = [r for r in recs if r["level"] == "sigun"]
    for s in siguns:
        s["pension"] = pension.get(s["name"])
    pt = next(s for s in siguns if s["name"] == "평택시")
    dongs = {r["name"]: r for r in recs if r["level"] == "dong" and r["parent"] == "평택시"}
    missing = [d for d in PT_DONGS if d not in dongs]
    if missing:
        raise RuntimeError(f"평택시 행정동을 찾지 못함: {missing}")

    p_tot = {"pop65": sum(v["pop65"] for v in pension.values()),
             "recipients": sum(v["recipients"] for v in pension.values())}
    p_tot["rate"] = round(p_tot["recipients"] / p_tot["pop65"] * 100, 2)
    sido["pension"] = p_tot

    kst = dt.timezone(dt.timedelta(hours=9))
    data = {
        "updated": dt.datetime.now(kst).strftime("%Y-%m-%d"),
        "baseMonth": f"{y}-{m}",
        "pensionMonth": p_month,
        "ageGroups": [g for g, _ in AGE_GROUPS],
        "gyeonggi": sido,
        "siguns": siguns,
        "pyeongtaek": pt,
        "ptDongs": [dongs[d] for d in PT_DONGS],
    }
    (BASE / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tpl = (BASE / "template.html").read_text(encoding="utf-8")
    body = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
    # claude.ai Artifact용(문서 골격은 게시 시 자동으로 붙음)
    (BASE / "artifact.html").write_text(body, encoding="utf-8")
    # GitHub Pages 등 일반 웹 호스팅용 완전한 문서
    page = ('<!doctype html>\n<html lang="ko">\n<head>\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            + body.replace("<style>", "<style>\nbody{margin:0}", 1).replace("\n<div class=\"wrap\">", "\n</head>\n<body>\n<div class=\"wrap\">", 1)
            + "\n</body>\n</html>\n")
    (BASE / "index.html").write_text(page, encoding="utf-8")
    print(f"완료: 인구 기준 {y}-{m}, 기초연금 기준 {p_month}, 갱신일 {data['updated']}")


if __name__ == "__main__":
    main()
