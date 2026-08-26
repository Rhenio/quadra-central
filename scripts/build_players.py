#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_players.py — gera data/players.json com o resumo Tennis Abstract
dos jogadores que têm jogo/aposta hoje na DB Oficial.

Fluxo:
  1. Lê a aba "DB Oficial" da planilha (export CSV público via gviz).
  2. Seleciona os jogadores das linhas de hoje (e amanhã, opcional) sem vencedor.
  3. Para cada jogador, baixa tennisabstract.com/jsplayers/<Slug>.js
     (tenta a variante WTA "<Slug>w.js" se a ATP não existir).
  4. Extrai o array `matchmx`, valida a estrutura e computa:
     W-L no ano (geral e por quadra), forma (últimos 10), melhores vitórias
     por ranking do adversário, piores derrotas, e agregados de saque/devolução.
  5. Escreve data/players.json (o site lê este arquivo).

Segurança contra estrutura desconhecida:
  - COLMAP abaixo é a ÚNICA fonte dos índices de coluna.
  - As colunas de identidade (data, W/L, adversário, ranking, placar) são
    validadas com regex/domínio; se falharem, o script ABORTA com dump da linha.
  - As colunas de estatística de saque são validadas por consistência
    (1ºs feitos <= 1ºs sacados <= pontos etc.); se falharem, o JSON sai com
    saque/devolucao = null e um aviso no log — nunca com número errado.
  - `python build_players.py --dump CristianGarin` imprime uma linha crua
    com índices para calibrar o COLMAP manualmente.

Config por variável de ambiente (todas têm default):
  SHEET_ID    id da planilha            (default: tennis_test_v3)
  SHEET_TAB   nome da aba               (default: DB Oficial)
  DAYS_AHEAD  incluir jogos de hoje+N   (default: 1)
  TZ_OFFSET   fuso em horas vs UTC      (default: -3, São Paulo)
"""

import ast
import datetime as dt
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
SHEET_ID   = os.environ.get("SHEET_ID", "1otLNM8g0D3yp44DlUyq5IR8dLj0hI_DFdFo7hjQ3JUQ")
SHEET_TAB  = os.environ.get("SHEET_TAB", "DB Oficial")
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "1"))
TZ_OFFSET  = int(os.environ.get("TZ_OFFSET", "-3"))

# Padrões candidatos de URL do arquivo por jogador. O script descobre sozinho
# qual funciona (sonda com um jogador conhecido) e grava no ta_cache.json.
TA_PATTERNS = [
    "https://www.tennisabstract.com/cgi-bin/jsmatches/{slug}.js",
    "https://www.tennisabstract.com/jsmatches/{slug}.js",
    "https://www.tennisabstract.com/jsplayers/{slug}.js",
    "https://www.tennisabstract.com/cgi-bin/jsplayers/{slug}.js",
]
PROBE_ATP = "JannikSinner"   # jogador que certamente existe, para a sondagem
PROBE_WTA = "IgaSwiatek"
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8",
    "Referer": "https://www.tennisabstract.com/",
}
SLEEP_S    = 2.0          # pausa entre requisições ao TA
TOP_N      = 3            # nº de melhores vitórias / piores derrotas

APELIDOS_PATH = "data/ta_apelidos.json"   # nome na planilha -> {"slug": ..., "tour": "atp"|"wta"}
CACHE_PATH    = "data/ta_cache.json"      # slug -> variante que funcionou ("atp"|"wta")
OUT_PATH      = "data/players.json"

# ----------------------------------------------------------------------------
# COLMAP — índices das colunas do array `matchmx` dos arquivos jsplayers.
# Se o TA mudar o layout (ou o chute inicial estiver errado), rode:
#     python scripts/build_players.py --dump CristianGarin
# compare com a página do jogador e ajuste os índices aqui.
# ----------------------------------------------------------------------------
COLMAP = {
    # identidade do jogo — validação DURA (aborta se não bater)
    "date":   0,    # yyyymmdd
    "tourney": 1,   # nome do torneio
    "surf":   2,    # Hard / Clay / Grass / Carpet
    "level":  3,    # nível (GS, M, A, C, F...)
    "wl":     4,    # "W" ou "L"
    "rank":   5,    # ranking do jogador na data
    "round":  8,    # F, SF, QF, R16...
    "score":  9,    # placar
    "opp":    11,   # nome do adversário
    "orank":  12,   # ranking do adversário

    # estatísticas de saque do JOGADOR — validação SUAVE (desliga se não bater)
    # (índices confirmados pelo dump de 26/08: [19]=flag, [20]=minutos, stats a partir de 21)
    "aces":   21,
    "dfs":    22,
    "pts":    23,   # pontos de saque
    "firsts": 24,   # 1º saque dentro
    "fwon":   25,   # pontos ganhos no 1º
    "swon":   26,   # pontos ganhos no 2º
    "games":  27,   # games de saque
    "saved":  28,   # break points salvos
    "chances": 29,  # break points enfrentados

    # mesmas estatísticas do ADVERSÁRIO (para devolução)
    "o_aces":   30,
    "o_dfs":    31,
    "o_pts":    32,
    "o_firsts": 33,
    "o_fwon":   34,
    "o_swon":   35,
    "o_games":  36,
    "o_saved":  37,
    "o_chances": 38,
}

SURF_PT = {"Hard": "Dura", "Clay": "Saibro", "Grass": "Grama", "Carpet": "Carpete"}

# ----------------------------------------------------------------------------
# util
# ----------------------------------------------------------------------------
def log(msg):
    print(msg, flush=True)

def today_local():
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET)).date()

def slugify(name: str) -> str:
    """'Cristián O'Connell-Jr' -> 'CristianOconnellJr' (padrão de URL do TA)."""
    s = unicodedata.normalize("NFD", name)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("'", "").replace("\u2019", "")   # O'Connell -> Oconnell
    parts = re.split(r"[\s\-]+", s.strip())
    return "".join(p[:1].upper() + p[1:].lower() for p in parts if p)

def fetch(url: str, timeout=30, headers=None):
    req = urllib.request.Request(url, headers=headers or BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def as_int(v):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None

# ----------------------------------------------------------------------------
# 1) jogadores do dia, a partir da planilha
# ----------------------------------------------------------------------------
def players_of_the_day():
    """Lê a DB Oficial via export CSV e devolve [(nome, torneio), ...] das
    linhas com data em [hoje, hoje+DAYS_AHEAD] e coluna Vencedor vazia."""
    import csv
    url = (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq"
           f"?tqx=out:csv&sheet={urllib.request.quote(SHEET_TAB)}")
    raw = fetch(url)
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        raise SystemExit("Planilha vazia ou inacessível (o link precisa estar "
                         "compartilhado como 'qualquer pessoa com o link').")

    # localiza a linha de cabeçalho procurando a coluna 'Data'
    header_i = next((i for i, r in enumerate(rows[:10])
                     if any(c.strip().lower() == "data" for c in r)), None)
    if header_i is None:
        raise SystemExit("Não achei o cabeçalho ('Data') nas 10 primeiras linhas da aba.")
    header = [c.strip().lower() for c in rows[header_i]]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        raise SystemExit(f"Coluna {names} não encontrada no cabeçalho: {header[:15]}")

    c_data, c_j1, c_j2 = col("data"), col("jogador 1", "j1"), col("jogador 2", "j2")
    c_win, c_tour = col("vencedor"), col("torneio")

    lo, hi = today_local(), today_local() + dt.timedelta(days=DAYS_AHEAD)
    wanted, seen = [], set()
    for r in rows[header_i + 1:]:
        if len(r) <= max(c_data, c_j1, c_j2, c_win):
            continue
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", r[c_data].strip())
        if not m:
            continue
        d = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if not (lo <= d <= hi) or r[c_win].strip():
            continue
        for name in (r[c_j1].strip(), r[c_j2].strip()):
            if name and name not in seen:
                seen.add(name)
                wanted.append((name, r[c_tour].strip()))
    return wanted

# ----------------------------------------------------------------------------
# 2) download + parse do arquivo do jogador
# ----------------------------------------------------------------------------
def try_url(url):
    """Baixa a URL e devolve (matchmx|None, motivo)."""
    try:
        body = fetch(url)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} {e.reason}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    m = re.search(r"var\s+matchmx\s*=\s*(\[\s*\[.*?\]\s*\])\s*;", body, re.S)
    if not m:
        snippet = body[:160].replace("\n", " ")
        return None, f"baixado ({len(body)} bytes) mas sem 'var matchmx'. Início: {snippet!r}"
    txt = m.group(1)
    txt = re.sub(r"\bnull\b", "None", txt)
    txt = re.sub(r"\btrue\b", "True", txt)
    txt = re.sub(r"\bfalse\b", "False", txt)
    try:
        return ast.literal_eval(txt), "ok"
    except (ValueError, SyntaxError) as e:
        return None, f"matchmx encontrado mas não parseável ({e})"

def discover_pattern(cache):
    """Descobre qual padrão de URL serve os arquivos, sondando jogadores
    conhecidos. Grava em cache['_pattern'] e devolve o padrão."""
    if cache.get("_pattern") in TA_PATTERNS:
        return cache["_pattern"]
    log("Descobrindo o padrão de URL do TA...")
    report = []
    for pat in TA_PATTERNS:
        for probe in (PROBE_ATP, PROBE_ATP + "w", PROBE_WTA, PROBE_WTA + "w"):
            url = pat.format(slug=probe)
            mx, why = try_url(url)
            report.append(f"  {url} -> {why}")
            log(report[-1])
            time.sleep(1.0)
            if mx:
                cache["_pattern"] = pat
                log(f"Padrão descoberto: {pat}")
                return pat
    log("\nERRO: nenhum padrão candidato serviu o matchmx. Resultados das sondagens acima.")
    raise SystemExit("Não foi possível localizar os arquivos de jogador no Tennis Abstract. "
                     "Envie o log acima para diagnóstico.")

def fetch_matchmx(slug: str, cache: dict, pattern: str):
    """Baixa o arquivo do jogador com o padrão descoberto. Tenta ATP (<Slug>)
    e WTA (<Slug>w), na ordem indicada pelo cache. Devolve (matchmx, tour)."""
    order = ["atp", "wta"]
    if cache.get(slug) == "wta":
        order = ["wta", "atp"]
    for tour in order:
        s = slug + ("" if tour == "atp" else "w")
        mx, why = try_url(pattern.format(slug=s))
        if mx:
            cache[slug] = tour
            return mx, tour
        log(f"    {s}: {why}")
    return None, None

# ----------------------------------------------------------------------------
# 3) validação do COLMAP
# ----------------------------------------------------------------------------
def validate_core(mx, who):
    """Valida as colunas de identidade. Aborta com dump se não baterem."""
    sample = mx[: min(len(mx), 40)]
    checks = {
        "date":  lambda v: re.fullmatch(r"\d{8}", str(v)) is not None,
        "wl":    lambda v: str(v) in ("W", "L"),
        "score": lambda v: re.search(r"\d", str(v)) is not None or "W/O" in str(v).upper(),
        "opp":   lambda v: re.search(r"[A-Za-z]", str(v)) is not None,
    }
    for field, ok in checks.items():
        i = COLMAP[field]
        good = sum(1 for r in sample if len(r) > i and ok(r[i]))
        if good / len(sample) < 0.9:
            log(f"\nERRO: coluna '{field}' (índice {i}) não validou para {who} "
                f"({good}/{len(sample)} linhas ok).")
            dump_row(mx[0])
            raise SystemExit(
                "O layout do matchmx difere do COLMAP. Ajuste os índices em "
                "scripts/build_players.py (bloco COLMAP) usando o dump acima.")

def stats_ok(mx):
    """Validação suave das colunas de saque: consistência aritmética."""
    rows = [r for r in mx[:60] if len(r) > COLMAP["o_chances"]]
    if not rows:
        return False
    good = 0
    for r in rows:
        pts, fi, fw, sw = (as_int(r[COLMAP[k]]) for k in ("pts", "firsts", "fwon", "swon"))
        sv, ch = as_int(r[COLMAP["saved"]]), as_int(r[COLMAP["chances"]])
        if None in (pts, fi, fw, sw, sv, ch):
            continue
        if 0 <= fw <= fi <= pts and 0 <= sw <= pts - fi and 0 <= sv <= ch:
            good += 1
    return good >= max(5, 0.7 * len(rows))

def dump_row(row):
    log("Linha crua do matchmx (índice: valor):")
    for i, v in enumerate(row):
        log(f"  [{i:2d}] {v!r}")

# ----------------------------------------------------------------------------
# 4) resumo do jogador
# ----------------------------------------------------------------------------
def summarize(mx, year, with_stats):
    g = lambda r, k: r[COLMAP[k]] if len(r) > COLMAP[k] else None
    yr = [r for r in mx if str(g(r, "date")).startswith(str(year))]
    yr.sort(key=lambda r: str(g(r, "date")))            # cronológico
    played = [r for r in yr if "W/O" not in str(g(r, "score")).upper()]

    w = sum(1 for r in played if g(r, "wl") == "W")
    l = len(played) - w
    by_surf = {}
    for r in played:
        s = SURF_PT.get(str(g(r, "surf")), str(g(r, "surf")) or "?")
        d = by_surf.setdefault(s, [0, 0])
        d[0 if g(r, "wl") == "W" else 1] += 1

    def fmt(r):
        d = str(g(r, "date"))
        rk = as_int(g(r, "orank"))
        return {
            "data": f"{d[6:8]}/{d[4:6]}",
            "torneio": g(r, "tourney"),
            "round": g(r, "round"),
            "adversario": g(r, "opp"),
            "rank_adv": rk,
            "placar": g(r, "score"),
        }

    wins   = sorted((r for r in played if g(r, "wl") == "W" and as_int(g(r, "orank"))),
                    key=lambda r: as_int(g(r, "orank")))[:TOP_N]
    losses = sorted((r for r in played if g(r, "wl") == "L" and as_int(g(r, "orank"))),
                    key=lambda r: -as_int(g(r, "orank")))[:TOP_N]

    out = {
        "ano": year,
        "jogos": {"v": w, "d": l, "por_quadra": {k: {"v": a, "d": b} for k, (a, b) in by_surf.items()}},
        "forma_ult10": [str(g(r, "wl")) for r in played[-10:]],
        "melhores_vitorias": [fmt(r) for r in wins],
        "piores_derrotas": [fmt(r) for r in losses],
        "saque": None,
        "devolucao": None,
    }

    if with_stats:
        tot = {k: 0 for k in ("aces", "dfs", "pts", "firsts", "fwon", "swon", "saved",
                              "chances", "o_aces", "o_pts", "o_firsts", "o_fwon",
                              "o_swon", "o_saved", "o_chances")}
        n = 0
        for r in played:
            vals = {k: as_int(g(r, k)) for k in tot}
            if vals["pts"] in (None, 0) or vals["o_pts"] in (None, 0):
                continue
            if any(v is None for v in vals.values()):
                continue
            for k, v in vals.items():
                tot[k] += v
            n += 1
        if n and tot["pts"] and tot["o_pts"]:
            pct = lambda a, b: round(100 * a / b, 1) if b else None
            out["saque"] = {
                "jogos_com_stats": n,
                "aces_pct": pct(tot["aces"], tot["pts"]),
                "df_pct": pct(tot["dfs"], tot["pts"]),
                "1st_in_pct": pct(tot["firsts"], tot["pts"]),
                "1st_won_pct": pct(tot["fwon"], tot["firsts"]),
                "2nd_won_pct": pct(tot["swon"], tot["pts"] - tot["firsts"]),
                "spw_pct": pct(tot["fwon"] + tot["swon"], tot["pts"]),
                "bp_salvos_pct": pct(tot["saved"], tot["chances"]),
            }
            out["devolucao"] = {
                "rpw_pct": pct(tot["o_pts"] - tot["o_fwon"] - tot["o_swon"], tot["o_pts"]),
                "vs_1st_pct": pct(tot["o_firsts"] - tot["o_fwon"], tot["o_firsts"]),
                "vs_2nd_pct": pct((tot["o_pts"] - tot["o_firsts"]) - tot["o_swon"],
                                  tot["o_pts"] - tot["o_firsts"]),
                "bp_convertidos_pct": pct(tot["o_chances"] - tot["o_saved"], tot["o_chances"]),
            }
    return out

# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--dump":
        c = {}
        mx, tour = fetch_matchmx(sys.argv[2], c, discover_pattern(c))
        if not mx:
            raise SystemExit("Não consegui baixar o matchmx desse slug.")
        log(f"Tour detectado: {tour} — {len(mx)} jogos no arquivo.")
        dump_row(mx[0])
        return

    apelidos = load_json(APELIDOS_PATH, {})
    cache    = load_json(CACHE_PATH, {})
    year     = today_local().year

    pattern = discover_pattern(cache)
    wanted = players_of_the_day()
    log(f"{len(wanted)} jogador(es) com jogo em aberto entre hoje e hoje+{DAYS_AHEAD}.")

    players, falhas = {}, []
    for name, tourney in wanted:
        ap = apelidos.get(name, {})
        slug = ap.get("slug") or slugify(name)
        log(f"  {name} -> {slug}")
        mx, tour = fetch_matchmx(slug, cache, pattern)
        time.sleep(SLEEP_S)
        if not mx:
            falhas.append(name)
            log("    NÃO ENCONTRADO no TA — adicione o slug correto em data/ta_apelidos.json")
            continue
        validate_core(mx, name)
        players[name] = {
            "slug": slug,
            "tour": tour,
            "ta_url": ("https://www.tennisabstract.com/cgi-bin/"
                       + ("player.cgi" if tour == "atp" else "wplayer.cgi")
                       + "?p=" + slug),
            **summarize(mx, year, with_stats=stats_ok(mx)),
        }
        if players[name]["saque"] is None:
            log("    aviso: colunas de saque não validaram — painel sai sem stats de saque/devolução.")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "ano": year,
            "falhas": falhas,
            "players": players,
        }, f, ensure_ascii=False, indent=1)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    log(f"OK — {len(players)} jogador(es) em {OUT_PATH}; {len(falhas)} falha(s).")

if __name__ == "__main__":
    main()
