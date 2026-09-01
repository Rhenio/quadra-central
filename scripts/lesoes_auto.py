#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lesoes_auto.py — Camada 1 do registro de lesões do Quadra Central.

Lê data/players.json (build_players.py) e data/lesoes.json (editorial, curado)
e produz data/lesoes_status.json com o semáforo consolidado por jogador.

Formato real de players.json:
  {"gerado_em": ..., "ano": ..., "falhas": [...], "players": {
     "Nome": {"slug":..., "tour":..., "jogos_recentes": [
        ["AAAAMMDD", "Adversário", "6-4 1-2 RET", "W"|"L"], ...
     ], "_atualizado": "AAAA-MM-DD", ...}}}

Sinais automáticos:
  1. RET  no placar + resultado "L"  -> o próprio jogador abandonou
     (com "W", quem abandonou foi o adversário — não conta contra ele)
  2. W/O  no placar + resultado "L"  -> desistiu antes de jogar
  3. GAP  — mais de GAP_DIAS sem jogar desde o último jogo registrado

Consolidação (a pior cor vence):
  RET/W.O. sofrido em <= 30 dias .................. vermelho
  RET/W.O. entre 31 e 90 dias, ou GAP ativo ....... amarelo
  nada em 90 dias ................................. verde
  Editorial rebaixa sempre; só melhora a cor se o registro
  trouxer "override": true (ex.: alta médica confirmada).

Uso no workflow, logo após o build_players.py:
    python scripts/lesoes_auto.py
(padrões: --players data/players.json --editorial data/lesoes.json
          --saida data/lesoes_status.json)
"""

import argparse
import json
import re
import sys
import unicodedata
from datetime import date, datetime

GAP_DIAS = 30
RET_VERMELHO = 30
RET_AMARELO = 90

RE_RET = re.compile(r"\bret\.?\b", re.IGNORECASE)
RE_WO = re.compile(r"\bw\.?/?o\.?\b|\bwalkover\b", re.IGNORECASE)
ORDEM = {"verde": 0, "amarelo": 1, "vermelho": 2}


def norm(s):
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFKD", s).replace("\xa0", " ")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split()).lower()


def parse_data(v):
    s = str(v or "")[:10]
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.replace("-", "" if fmt == "%Y%m%d" else "-"), fmt).date()
        except ValueError:
            continue
    return None


def le_jogo(j):
    """Aceita o formato lista [data, adv, placar, resultado] e, por robustez, dict."""
    if isinstance(j, (list, tuple)) and len(j) >= 4:
        return parse_data(j[0]), str(j[2]), str(j[3]).upper()
    if isinstance(j, dict):
        return (parse_data(j.get("data") or j.get("date")),
                str(j.get("placar") or j.get("score") or ""),
                str(j.get("resultado") or j.get("result") or "").upper())
    return None, "", ""


def sinais_do_jogador(dados, hoje):
    sinais, datas = [], []
    for j in dados.get("jogos_recentes") or []:
        d, placar, res = le_jogo(j)
        if d:
            datas.append(d)
        perdeu = res.startswith(("L", "D"))
        if RE_RET.search(placar) and perdeu:
            sinais.append({"tipo": "ret", "data": d.isoformat() if d else None})
        if RE_WO.search(placar) and perdeu:
            sinais.append({"tipo": "wo", "data": d.isoformat() if d else None})
    # dedup (o TA às vezes repete o mesmo jogo)
    vistos, unicos = set(), []
    for s in sinais:
        k = (s["tipo"], s["data"])
        if k not in vistos:
            vistos.add(k)
            unicos.append(s)
    sinais = unicos
    ultimo = max(datas) if datas else None
    if ultimo and (hoje - ultimo).days > GAP_DIAS:
        sinais.append({"tipo": "gap", "data": ultimo.isoformat(),
                       "dias": (hoje - ultimo).days})
    return sinais, ultimo


def cor_automatica(sinais, hoje):
    cor = "verde"
    for s in sinais:
        if s["tipo"] in ("ret", "wo") and s.get("data"):
            dias = (hoje - date.fromisoformat(s["data"])).days
            if dias <= RET_VERMELHO:
                cor = "vermelho"
            elif dias <= RET_AMARELO and ORDEM[cor] < ORDEM["amarelo"]:
                cor = "amarelo"
        elif s["tipo"] == "gap" and ORDEM[cor] < ORDEM["amarelo"]:
            cor = "amarelo"
    return cor


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", default="data/players.json")
    ap.add_argument("--editorial", default="data/lesoes.json")
    ap.add_argument("--saida", default="data/lesoes_status.json")
    args = ap.parse_args()

    hoje = date.today()

    with open(args.players, encoding="utf-8") as f:
        pj = json.load(f)
    mapa = pj.get("players", pj) if isinstance(pj, dict) else {}

    try:
        with open(args.editorial, encoding="utf-8") as f:
            editorial = json.load(f).get("jogadores", {})
    except FileNotFoundError:
        editorial = {}
    ed_por_nome = {norm(k): (k, v) for k, v in editorial.items()}

    saida = {"gerado_em": hoje.isoformat(), "jogadores": {}}
    for nome, dados in mapa.items():
        if not isinstance(dados, dict):
            continue
        sinais, ultimo = sinais_do_jogador(dados, hoje)
        cor_auto = cor_automatica(sinais, hoje)

        cor_final, resumo, area, eventos = cor_auto, "", "", []
        ed = ed_por_nome.get(norm(nome))
        if ed:
            reg = ed[1]
            cor_ed = reg.get("status", "verde")
            if reg.get("override"):
                cor_final = cor_ed
            else:
                cor_final = cor_auto if ORDEM[cor_auto] >= ORDEM[cor_ed] else cor_ed
            resumo = reg.get("resumo", "")
            area = reg.get("area", "")
            eventos = reg.get("eventos", [])

        if cor_final == "verde" and not sinais and not ed:
            continue  # verde sem nota não entra no arquivo (mantém o JSON enxuto)

        saida["jogadores"][nome] = {
            "status": cor_final,
            "status_auto": cor_auto,
            "sinais_auto": sinais,
            "ultimo_jogo": ultimo.isoformat() if ultimo else None,
            "dias_sem_jogar": (hoje - ultimo).days if ultimo else None,
            "area": area,
            "resumo": resumo,
            "eventos": eventos,
        }

    # jogadores só do editorial (desistentes etc., sem jogos abertos)
    presentes = {norm(n) for n in saida["jogadores"]}
    for nome_norm, (nome, reg) in ed_por_nome.items():
        if nome_norm not in presentes:
            saida["jogadores"][nome] = {
                "status": reg.get("status", "verde"),
                "status_auto": None, "sinais_auto": [],
                "ultimo_jogo": None, "dias_sem_jogar": None,
                "area": reg.get("area", ""), "resumo": reg.get("resumo", ""),
                "eventos": reg.get("eventos", []),
            }

    with open(args.saida, "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, indent=1)

    tot = len(saida["jogadores"])
    verm = sum(1 for j in saida["jogadores"].values() if j["status"] == "vermelho")
    amar = sum(1 for j in saida["jogadores"].values() if j["status"] == "amarelo")
    print(f"lesoes_auto: {tot} jogadores no arquivo — {verm} vermelho, {amar} amarelo, "
          f"{tot - verm - amar} verde com nota -> {args.saida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
