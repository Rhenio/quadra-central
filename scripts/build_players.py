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
import html as htmllib
import unicodedata
import urllib.error
import urllib.request

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
SHEET_ID   = os.environ.get("SHEET_ID", "1otLNM8g0D3yp44DlUyq5IR8dLj0hI_DFdFo7hjQ3JUQ")
SHEET_TAB  = os.environ.get("SHEET_TAB", "DB Oficial")
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "1"))
DAYS_BACK  = int(os.environ.get("DAYS_BACK", "3"))   # retro: placares p/ o Histórico
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

    hoje = today_local()
    lo, hi = hoje, hoje + dt.timedelta(days=DAYS_AHEAD)
    retro_lo = hoje - dt.timedelta(days=DAYS_BACK)
    wanted = {}   # nome -> {"tourney":..., "ate": None (sempre buscar) | date do jogo mais recente}
    for r in rows[header_i + 1:]:
        if len(r) <= max(c_data, c_j1, c_j2, c_win):
            continue
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", r[c_data].strip())
        if not m:
            continue
        d = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        aberto = lo <= d <= hi and not r[c_win].strip()
        retro  = retro_lo <= d < hoje                      # jogos passados: queremos o placar
        if not (aberto or retro):
            continue
        for name in (r[c_j1].strip(), r[c_j2].strip()):
            if not name:
                continue
            w = wanted.setdefault(name, {"tourney": r[c_tour].strip(), "ate": d})
            if aberto:
                w["ate"] = None                            # jogo em aberto: busca sempre
            elif w["ate"] is not None and d > w["ate"]:
                w["ate"] = d
    return [(n, w["tourney"], w["ate"]) for n, w in wanted.items()]

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


_FRAG_HDR_LOGGED = False

MESES = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}

def _parse_date(s):
    """Aceita 'yyyymmdd', 'dd-Mon-yyyy' (com hífens especiais) e 'yyyy-mm-dd'."""
    t = str(s).strip()
    for h in ("\u2011", "\u2010", "\u2012", "\u2013", "&#8209;"):
        t = t.replace(h, "-")
    if re.fullmatch(r"\d{8}", t):
        return t
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3,})-(\d{4})", t)
    if m and m.group(2)[:3].lower() in MESES:
        return f"{m.group(3)}{MESES[m.group(2)[:3].lower()]:02d}{int(m.group(1)):02d}"
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    return None

def _cells(tr):
    return [htmllib.unescape(re.sub(r"<[^>]+>", " ", c)).replace("\xa0", " ").strip()
            for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S | re.I)]

def _frag_matches(body):
    """Extrai partidas do HTML pré-renderizado (var player_frag = `...`).
    Mapeia colunas pelo CABEÇALHO da tabela, não por posição. Devolve
    (lista de dicts, diagnostico) — diagnostico preenchido se nada parseou."""
    m = re.search(r"var\s+player_frag\s*=\s*`(.*)`", body, re.S)
    if not m:
        return [], None
    doc = m.group(1)
    out, diag = [], None
    for tbl in re.findall(r"<table.*?</table>", doc, re.S | re.I):
        trs = re.findall(r"<tr[^>]*>.*?</tr>", tbl, re.S | re.I)
        if len(trs) < 2:
            continue
        head = [h.lower() for h in _cells(trs[0])]
        if not any("date" in h for h in head) or not any("score" in h for h in head):
            continue

        def col(*names):
            for n in names:
                for i, h in enumerate(head):
                    if h == n:
                        return i
            for n in names:
                for i, h in enumerate(head):
                    if n in h:
                        return i
            return None

        global _FRAG_HDR_LOGGED
        if not _FRAG_HDR_LOGGED:
            _FRAG_HDR_LOGGED = True
            log(f"    [frag] cabeçalho completo da tabela: {head}")
        c_date, c_score = col("date"), col("score")
        c_tour = col("tournament", "tourney", "event")
        c_surf, c_rd, c_vrk = col("surf"), col("rd", "round"), col("vrk", "vrank")
        pcts = {"aces_pct": col("a%"), "df_pct": col("df%"), "1st_in_pct": col("1stin"),
                "1st_won_pct": col("1st%"), "2nd_won_pct": col("2nd%"),
                "bp_salvos_pct": col("bpsvd", "bpsaved", "bps"), "rpw_pct": col("rpw", "rtnpw", "rtn%")}
        parsed_antes = len(out)
        for tr in trs[1:]:
            cells = _cells(tr)
            if c_date is None or c_date >= len(cells):
                continue
            d = _parse_date(cells[c_date])
            if not d:
                continue
            # célula do confronto: "Vencedor d. Perdedor" (jogos futuros usam "vs")
            conf = None
            for c in cells:
                if re.search(r"(?:^|\s)d\.\s", c) or re.search(r"(?:^|\s)vs\s", c):
                    conf = re.sub(r"\s+", " ", c).strip()
                    break
            if not conf or " d. " not in f" {conf} ":
                continue  # sem resultado (jogo futuro "vs") ou linha sem confronto
            left, right = re.split(r"(?:^|\s)d\.\s", " " + conf, maxsplit=1)
            tag = r"\[[A-Z]{3}\]"
            l_has, r_has = bool(re.search(tag, left)), bool(re.search(tag, right))
            if l_has == r_has:
                continue  # ambíguo — não arrisca
            # o lado COM a tag de país é o adversário; o jogador é o lado sem tag
            if r_has:
                wl, opp_side = "W", right   # jogador (sem tag) venceu, à esquerda do d.
            else:
                wl, opp_side = "L", left    # adversário venceu
            opp = re.sub(tag, "", opp_side)
            opp = re.sub(r"\([^)]*\)", "", opp)       # remove seed/entry: (7), (Q), (WC)...
            opp = re.sub(r"\s+", " ", opp).strip()
            if not opp:
                continue
            orank = ""
            if c_vrk is not None and c_vrk < len(cells) and cells[c_vrk].strip().isdigit():
                orank = cells[c_vrk].strip()
            rec = {"date": d,
                   "score": cells[c_score] if c_score is not None and c_score < len(cells) else "",
                   "tourney": cells[c_tour] if c_tour is not None and c_tour < len(cells) else "",
                   "surf": cells[c_surf] if c_surf is not None and c_surf < len(cells) else "",
                   "round": cells[c_rd] if c_rd is not None and c_rd < len(cells) else "",
                   "opp": opp, "orank": orank, "wl": wl, "stats": {}}
            for k, i in pcts.items():
                if i is None or i >= len(cells):
                    continue
                v = cells[i].strip()
                fr = re.fullmatch(r"(\d+)\s*/\s*(\d+)", v)
                if fr:
                    a, b = int(fr.group(1)), int(fr.group(2))
                    rec.setdefault("fracs", {})[k] = (a, b)
                    if b:
                        rec["stats"][k] = round(100 * a / b, 1)
                    continue
                try:
                    rec["stats"][k] = float(v.replace("%", ""))
                except ValueError:
                    pass
            st = rec["stats"]
            if all(x in st for x in ("1st_in_pct", "1st_won_pct", "2nd_won_pct")):
                fi = st["1st_in_pct"]
                st["spw_pct"] = round((fi * st["1st_won_pct"]
                                       + (100 - fi) * st["2nd_won_pct"]) / 100, 1)
            out.append(rec)
        if len(out) == parsed_antes and diag is None:
            diag = ("cabeçalho=" + repr(head[:14]) +
                    " | 1ª linha=" + repr(_cells(trs[1])[:14] if len(trs) > 1 else []))
    if not out and diag is None:
        diag = "frag presente mas nenhuma tabela com Date+Score"
    return out, diag

def _frag_to_row(rec):
    r = [""] * 44
    r[COLMAP["date"]] = rec["date"]
    r[COLMAP["tourney"]] = rec["tourney"]
    r[COLMAP["surf"]] = rec["surf"]
    r[COLMAP["wl"]] = rec["wl"]
    r[COLMAP["round"]] = rec["round"]
    r[COLMAP["score"]] = rec["score"]
    r[COLMAP["opp"]] = rec["opp"]
    r[COLMAP["orank"]] = rec["orank"]
    return r

def _frag_stats_mean(recs, year):
    """Agrega as stats por jogo do frag: porcentagens por média simples;
    break points por soma de frações (salvos totais / enfrentados totais)."""
    ys = [r for r in recs
          if str(r["date"]).startswith(str(year)) and r["stats"]
          and "W/O" not in str(r["score"]).upper()]
    if not ys:
        return None, None
    stats = [r["stats"] for r in ys]
    mean = lambda k: (round(sum(d[k] for d in stats if k in d)
                            / max(1, sum(1 for d in stats if k in d)), 1)
                      if any(k in d for d in stats) else None)
    bp_a = sum(r.get("fracs", {}).get("bp_salvos_pct", (0, 0))[0] for r in ys)
    bp_b = sum(r.get("fracs", {}).get("bp_salvos_pct", (0, 0))[1] for r in ys)
    bp_salvos = round(100 * bp_a / bp_b, 1) if bp_b else mean("bp_salvos_pct")
    saque = {"jogos_com_stats": len(stats), "aces_pct": mean("aces_pct"), "df_pct": mean("df_pct"),
             "1st_in_pct": mean("1st_in_pct"), "1st_won_pct": mean("1st_won_pct"),
             "2nd_won_pct": mean("2nd_won_pct"), "spw_pct": mean("spw_pct"), "hold_pct": None,
             "bp_salvos_pct": bp_salvos}
    devol = {"rpw_pct": mean("rpw_pct"), "vs_1st_pct": None, "vs_2nd_pct": None,
             "brk_pct": None, "bp_convertidos_pct": None}
    return saque, devol

def discover_sources(cache):
    """Descobre os arquivos de dados perguntando à página oficial do jogador
    e devolve uma lista única de modelos de URL, na ordem de preferência:
    matchmx (contagens brutas) antes do fragmento HTML."""
    src = cache.get("_sources")
    if isinstance(src, list) and src:
        return src
    cache.pop("_templates", None)
    cache.pop("_pattern", None)
    descobertos = []
    for cgi, probe in (("player.cgi", PROBE_ATP), ("wplayer.cgi", PROBE_WTA)):
        url = f"https://www.tennisabstract.com/cgi-bin/{cgi}?p={probe}"
        log(f"Descobrindo fontes de dados em {url} ...")
        try:
            page = fetch(url)
        except Exception as e:
            log(f"  página inacessível ({e})")
            continue
        for f in sorted(set(re.findall(rf"[\"'=/ ]([^\"'<> ]*{probe}[A-Za-z0-9_]*\.js)", page))):
            if f.startswith("http"):
                absu = f
            elif f.startswith("//"):
                absu = "https:" + f
            elif f.startswith("/"):
                absu = "https://www.tennisabstract.com" + f
            else:
                absu = "https://www.tennisabstract.com/" + f.lstrip("./")
            tpl = absu.replace(probe, "{slug}")
            if tpl not in descobertos:
                descobertos.append(tpl)
                log(f"  fonte: {tpl}")
        time.sleep(1.0)
    fixos = ["https://www.tennisabstract.com/jsmatches/{slug}.js",
             "https://www.tennisabstract.com/jsmatches/{slug}w.js"]
    src = fixos + [t for t in descobertos if t not in fixos]
    cache["_sources"] = src
    return src

def fetch_player(slug: str, sources: list):
    """Baixa todas as fontes do jogador; funde linhas de matchmx e partidas do
    fragmento HTML (sem duplicar); devolve (rows, frag_recs, tour, motivos)."""
    rows, frag_recs, motivos = [], [], []
    seen = set()
    tour = None

    def key(date, opp, score):
        return (str(date), re.sub(r"\D", "", str(score)),
                str(opp).split()[-1].lower() if str(opp).split() else "")

    for tpl in sources:
        url = tpl.format(slug=slug)
        try:
            body = fetch(url)
        except urllib.error.HTTPError as e:
            motivos.append(f"{url.rsplit('/', 1)[-1]}: HTTP {e.code}")
            time.sleep(SLEEP_S)
            continue
        except Exception as e:
            motivos.append(f"{url.rsplit('/', 1)[-1]}: {type(e).__name__}")
            time.sleep(SLEEP_S)
            continue
        if tour is None:
            head = body[:4000]
            if "wplayer.cgi" in head:
                tour = "wta"
            elif "player.cgi" in head:
                tour = "atp"
        arrays = _match_arrays(body)
        if arrays:
            n_novos = 0
            for _, arr in arrays:
                for r in arr:
                    if not r or len(r) <= COLMAP["score"]:
                        continue
                    k = key(r[COLMAP["date"]] if len(r) > COLMAP["date"] else r[0],
                            r[COLMAP["opp"]] if len(r) > COLMAP["opp"] else "",
                            r[COLMAP["score"]])
                    if k in seen:
                        continue
                    seen.add(k)
                    rows.append(r)
                    n_novos += 1
            motivos.append(f"{url.rsplit('/', 1)[-1]}: matchmx {n_novos} jogos")
        else:
            recs, diag = _frag_matches(body)
            if recs:
                n_novos = 0
                for rec in recs:
                    k = key(rec["date"], rec["opp"], rec["score"])
                    if k in seen:
                        continue
                    seen.add(k)
                    rows.append(_frag_to_row(rec))
                    frag_recs.append(rec)
                    n_novos += 1
                motivos.append(f"{url.rsplit('/', 1)[-1]}: frag {n_novos} jogos")
            elif diag:
                motivos.append(f"{url.rsplit('/', 1)[-1]}: FRAG NÃO PARSEADO -> {diag}")
            else:
                motivos.append(f"{url.rsplit('/', 1)[-1]}: sem dados")
        time.sleep(SLEEP_S)
    return rows, frag_recs, (tour or "atp"), motivos

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

    corte = (today_local() - dt.timedelta(days=45)).strftime("%Y%m%d")
    recentes = [[str(g(r, "date")), str(g(r, "opp")), str(g(r, "score")), str(g(r, "wl"))]
                for r in played if str(g(r, "date")) >= corte][-40:]

    out = {
        "ano": year,
        "jogos": {"v": w, "d": l, "por_quadra": {k: {"v": a, "d": b} for k, (a, b) in by_surf.items()}},
        "forma_ult10": [str(g(r, "wl")) for r in played[-10:]],
        "jogos_recentes": recentes,
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
        rows, frags, tour, motivos = fetch_player(sys.argv[2], discover_sources(c))
        for m in motivos:
            log("  " + m)
        if not rows:
            raise SystemExit("Nenhuma fonte devolveu jogos para esse slug.")
        log(f"Tour: {tour} — {len(rows)} jogos fundidos.")
        dump_row(rows[0])
        return

    apelidos = load_json(APELIDOS_PATH, {})
    cache    = load_json(CACHE_PATH, {})
    antigos  = load_json(OUT_PATH, {}).get("players", {})
    year     = today_local().year

    sources = discover_sources(cache)
    wanted = players_of_the_day()
    abertos = sum(1 for _, _, ate in wanted if ate is None)
    log(f"{len(wanted)} jogador(es): {abertos} com jogo em aberto, "
        f"{len(wanted) - abertos} retroativos (últimos {DAYS_BACK} dias, p/ placares).")

    players, falhas = {}, []
    for name, tourney, ate in wanted:
        if ate is not None:
            ex = antigos.get(name)
            if ex and ex.get("jogos_recentes") and str(ex.get("_atualizado", "")) > ate.isoformat():
                continue   # retroativo já coberto por uma busca posterior ao jogo
        ap = apelidos.get(name, {})
        slug = ap.get("slug") or slugify(name)
        log(f"  {name} -> {slug}")
        rows, frags, tour, motivos = fetch_player(slug, sources)
        cache[name] = tour
        if not rows:
            falhas.append(name)
            for mo in motivos:
                log(f"    {mo}")
            log("    NÃO ENCONTRADO no TA — adicione o slug correto em data/ta_apelidos.json")
            continue
        log("    " + " | ".join(motivos))
        validate_core(rows, name)
        datas = sorted(str(r[COLMAP["date"]]) for r in rows if len(r) > COLMAP["date"])
        resumo = summarize(rows, year, with_stats=stats_ok(rows))
        origem_stats = "matchmx"
        if resumo["saque"] is None and frags:
            sq, dv = _frag_stats_mean(frags, year)
            if sq:
                resumo["saque"], resumo["devolucao"] = sq, dv
                origem_stats = "frag (média por jogo)"
        log(f"    ok ({tour}): {len(rows)} jogos fundidos, de {datas[0]} a {datas[-1]}; "
            f"{resumo['jogos']['v']}V-{resumo['jogos']['d']}D em {year}; "
            f"stats: {origem_stats if resumo['saque'] else 'indisponíveis'}")
        players[name] = {
            "slug": slug,
            "tour": tour,
            "ta_url": ("https://www.tennisabstract.com/cgi-bin/"
                       + ("player.cgi" if tour == "atp" else "wplayer.cgi")
                       + "?p=" + slug),
            **resumo,
        }

    # cumulativo: mantém jogadores de dias anteriores (até 30 dias) para a aba Histórico
    hoje_s = today_local().isoformat()
    for p in players.values():
        p["_atualizado"] = hoje_s
    limite = (today_local() - dt.timedelta(days=30)).isoformat()
    mantidos = 0
    for nome, p in antigos.items():
        if nome in players:
            continue
        if str(p.get("_atualizado", "")) >= limite:
            players[nome] = p
            mantidos += 1
    log(f"{mantidos} jogador(es) de dias anteriores mantidos no players.json (janela de 30 dias).")

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
