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

PROBE_ATP = "JannikSinner"   # jogadores-sonda para descobrir as fontes
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
def _match_arrays(body):
    """Extrai TODOS os arrays JS do arquivo cujas linhas parecem partidas
    (data yyyymmdd na 1ª coluna). O TA pode separar carreira e temporada
    atual em arrays distintos (ex.: matchmx e outro irmão)."""
    out = []
    for name, txt in re.findall(r"var\s+(\w+)\s*=\s*(\[\s*\[.*?\]\s*\])\s*;", body, re.S):
        t = re.sub(r"\bnull\b", "None", txt)
        t = re.sub(r"\btrue\b", "True", t)
        t = re.sub(r"\bfalse\b", "False", t)
        try:
            arr = ast.literal_eval(t)
        except (ValueError, SyntaxError):
            continue
        if not (arr and isinstance(arr[0], (list, tuple))):
            continue
        sample = arr[: min(len(arr), 40)]
        datelike = sum(1 for r in sample
                       if r and re.fullmatch(r"\d{8}", str(r[0]) if r else ""))
        if datelike / len(sample) >= 0.8:
            out.append((name, arr))
    return out

def try_url(url):
    """Baixa a URL e devolve (linhas_de_partida|None, motivo)."""
    try:
        body = fetch(url)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} {e.reason}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    arrays = _match_arrays(body)
    if not arrays:
        snippet = body[:160].replace("\n", " ")
        return None, f"baixado ({len(body)} bytes) sem array de partidas. Início: {snippet!r}"
    rows = [r for _, arr in arrays for r in arr]
    nomes = ", ".join(f"{n}({len(a)})" for n, a in arrays)
    return rows, f"ok [{nomes}]"

def discover_templates(cache):
    """Descobre os modelos de URL dos arquivos de dados perguntando à própria
    página do jogador: baixa o player.cgi/wplayer.cgi de um jogador conhecido,
    extrai todo caminho .js que contenha o nome dele e o transforma em modelo
    com {slug}. Grava em cache['_templates']."""
    tpls = cache.get("_templates")
    if isinstance(tpls, dict) and tpls.get("atp"):
        return tpls
    tpls = {"atp": [], "wta": []}
    fallback = "https://www.tennisabstract.com/jsmatches/{slug}.js"
    for tour, cgi, probe in (("atp", "player.cgi", PROBE_ATP),
                             ("wta", "wplayer.cgi", PROBE_WTA)):
        url = f"https://www.tennisabstract.com/cgi-bin/{cgi}?p={probe}"
        log(f"Descobrindo fontes de dados ({tour}) em {url} ...")
        try:
            html = fetch(url)
        except Exception as e:
            log(f"  página inacessível ({e}); usando padrão conhecido.")
            html = ""
        found = set(re.findall(rf"[\"'=/ ]([^\"'<> ]*{probe}[A-Za-z0-9_]*\.js)", html))
        for f in sorted(found):
            if f.startswith("http"):
                absu = f
            elif f.startswith("//"):
                absu = "https:" + f
            elif f.startswith("/"):
                absu = "https://www.tennisabstract.com" + f
            else:
                absu = "https://www.tennisabstract.com/" + f.lstrip("./")
            tpl = absu.replace(probe, "{slug}")
            if tpl not in tpls[tour]:
                tpls[tour].append(tpl)
                log(f"  fonte: {tpl}")
        # o padrão que já sabemos funcionar entra sempre como garantia
        fb = fallback if tour == "atp" else fallback.replace("{slug}", "{slug}w")
        for cand in (fallback, fb):
            if cand not in tpls[tour]:
                tpls[tour].append(cand)
        time.sleep(1.0)
    cache["_templates"] = tpls
    return tpls

def fetch_matchmx(slug: str, cache: dict, templates: dict):
    """Baixa TODAS as fontes conhecidas do jogador e funde as partidas,
    removendo duplicatas. Tenta o tour indicado pelo cache primeiro."""
    order = ["atp", "wta"]
    if cache.get(slug) == "wta":
        order = ["wta", "atp"]
    for tour in order:
        merged, seen, motivos = [], set(), []
        for tpl in templates.get(tour, []):
            rows, why = try_url(tpl.format(slug=slug))
            motivos.append(f"{tpl.format(slug=slug).rsplit('/',1)[-1]}: {why}")
            time.sleep(SLEEP_S)
            if not rows:
                continue
            for r in rows:
                if not r:
                    continue
                key = (str(r[0]), str(r[COLMAP["opp"]]) if len(r) > COLMAP["opp"] else "",
                       str(r[COLMAP["score"]]) if len(r) > COLMAP["score"] else "")
                if key in seen:
                    continue
                seen.add(key)
                merged.append(r)
        if merged:
            cache[slug] = tour
            log("    " + " | ".join(motivos))
            return merged, tour
        if tour == order[-1]:
            for m in motivos:
                log(f"    {m}")
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
        tot = {k: 0 for k in ("aces", "dfs", "pts", "firsts", "fwon", "swon", "games",
                              "saved", "chances", "o_aces", "o_pts", "o_firsts", "o_fwon",
                              "o_swon", "o_games", "o_saved", "o_chances")}
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
            quebras_sofridas = tot["chances"] - tot["saved"]
            quebras_feitas   = tot["o_chances"] - tot["o_saved"]
            out["saque"] = {
                "jogos_com_stats": n,
                "aces_pct": pct(tot["aces"], tot["pts"]),
                "df_pct": pct(tot["dfs"], tot["pts"]),
                "1st_in_pct": pct(tot["firsts"], tot["pts"]),
                "1st_won_pct": pct(tot["fwon"], tot["firsts"]),
                "2nd_won_pct": pct(tot["swon"], tot["pts"] - tot["firsts"]),
                "spw_pct": pct(tot["fwon"] + tot["swon"], tot["pts"]),
                "hold_pct": pct(tot["games"] - quebras_sofridas, tot["games"]),
                "bp_salvos_pct": pct(tot["saved"], tot["chances"]),
            }
            out["devolucao"] = {
                "rpw_pct": pct(tot["o_pts"] - tot["o_fwon"] - tot["o_swon"], tot["o_pts"]),
                "vs_1st_pct": pct(tot["o_firsts"] - tot["o_fwon"], tot["o_firsts"]),
                "vs_2nd_pct": pct((tot["o_pts"] - tot["o_firsts"]) - tot["o_swon"],
                                  tot["o_pts"] - tot["o_firsts"]),
                "brk_pct": pct(quebras_feitas, tot["o_games"]),
                "bp_convertidos_pct": pct(quebras_feitas, tot["o_chances"]),
            }
    return out

# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--dump":
        c = {}
        mx, tour = fetch_matchmx(sys.argv[2], c, discover_templates(c))
        if not mx:
            raise SystemExit("Não consegui baixar o matchmx desse slug.")
        log(f"Tour detectado: {tour} — {len(mx)} jogos no arquivo.")
        dump_row(mx[0])
        return

    apelidos = load_json(APELIDOS_PATH, {})
    cache    = load_json(CACHE_PATH, {})
    year     = today_local().year

    cache.pop("_pattern", None)   # formato antigo do cache
    templates = discover_templates(cache)
    wanted = players_of_the_day()
    log(f"{len(wanted)} jogador(es) com jogo em aberto entre hoje e hoje+{DAYS_AHEAD}.")

    players, falhas = {}, []
    for name, tourney in wanted:
        ap = apelidos.get(name, {})
        slug = ap.get("slug") or slugify(name)
        log(f"  {name} -> {slug}")
        mx, tour = fetch_matchmx(slug, cache, templates)
        if not mx:
            falhas.append(name)
            log("    NÃO ENCONTRADO no TA — adicione o slug correto em data/ta_apelidos.json")
            continue
        validate_core(mx, name)
        datas = sorted(str(r[COLMAP["date"]]) for r in mx if len(r) > COLMAP["date"])
        resumo = summarize(mx, year, with_stats=stats_ok(mx))
        log(f"    ok ({tour}): {len(mx)} jogos no arquivo, de {datas[0]} a {datas[-1]}; "
            f"{resumo['jogos']['v']}V-{resumo['jogos']['d']}D em {year}")
        if resumo["jogos"]["v"] + resumo["jogos"]["d"] == 0:
            log(f"    ATENÇÃO: nenhum jogo de {year} no arquivo — pode estar desatualizado no TA.")
        players[name] = {
            "slug": slug,
            "tour": tour,
            "ta_url": ("https://www.tennisabstract.com/cgi-bin/"
                       + ("player.cgi" if tour == "atp" else "wplayer.cgi")
                       + "?p=" + slug),
            **resumo,
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
