/* ta.js — integração Tennis Abstract no site de apostas.
 *
 * O que faz:
 *   1. Transforma nomes de jogadores em links para a página deles no TA.
 *   2. Renderiza, dentro do card da aposta, um painel "Contexto" com os dados
 *      de data/players.json (gerado diariamente pela GitHub Action).
 *
 * Integração mínima (2 passos):
 *   a) Nos elementos que mostram nome de jogador, adicione data-player:
 *        <span class="j1-nome" data-player="Elias Ymer">Elias Ymer</span>
 *   b) No fim do card, um container:  <div class="ta-panel" data-match="Elias Ymer|Cristian Garin"></div>
 *      e, depois de montar os cards:  TA.init();
 *
 * Se o site monta os cards via JS, chame TA.init() de novo após cada render.
 */
(function () {
  "use strict";

  var DATA_URL = "data/players.json";
  var cache = null;

  /* ---- slug idêntico ao do script Python (fallback p/ jogador fora do JSON) */
  function slugify(name) {
    var s = name.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    s = s.replace(/['\u2019]/g, ""); // O'Connell -> Oconnell
    return s
      .split(/[\s\-]+/)
      .filter(Boolean)
      .map(function (p) { return p.charAt(0).toUpperCase() + p.slice(1).toLowerCase(); })
      .join("");
  }

  function taUrl(name, tour) {
    var p = cache && cache.players && cache.players[name];
    if (p && p.ta_url) return p.ta_url;
    var cgi = tour === "wta" ? "wplayer.cgi" : "player.cgi";
    return "https://www.tennisabstract.com/cgi-bin/" + cgi + "?p=" + slugify(name);
  }

  /* ---- 1. links nos nomes ------------------------------------------------ */
  function linkNames(root) {
    (root || document).querySelectorAll("[data-player]").forEach(function (el) {
      if (el.dataset.taLinked) return;
      el.dataset.taLinked = "1";
      var name = el.dataset.player;
      var a = document.createElement("a");
      a.href = taUrl(name, el.dataset.tour);
      a.target = "_blank";
      a.rel = "noopener";
      a.className = "ta-link";
      a.title = "Abrir no Tennis Abstract";
      while (el.firstChild) a.appendChild(el.firstChild);
      el.appendChild(a);
    });
  }

  /* ---- 2. painel de contexto -------------------------------------------- */
  function fmtRecord(j) { return j.v + "–" + j.d; }

  function fmtForma(seq) {
    return seq.map(function (r) {
      return '<i class="ta-f ta-f-' + r.toLowerCase() + '">' + (r === "W" ? "V" : "D") + "</i>";
    }).join("");
  }

  function fmtJogos(list, verbo) {
    if (!list || !list.length) return "<li class='ta-mut'>—</li>";
    return list.map(function (m) {
      return "<li>" + verbo + " <b>#" + m.rank_adv + " " + m.adversario + "</b> " +
        m.placar + " <span class='ta-mut'>(" + m.torneio + " " + m.round + ", " + m.data + ")</span></li>";
    }).join("");
  }

  function statRow(label, a, b, lowerBetter) {
    var va = a == null ? "—" : a + "%";
    var vb = b == null ? "—" : b + "%";
    var aWins = a != null && b != null && (lowerBetter ? a < b : a > b);
    var bWins = a != null && b != null && (lowerBetter ? b < a : b > a);
    return "<tr><td class='ta-num" + (aWins ? " ta-best" : "") + "'>" + va + "</td>" +
           "<th>" + label + "</th>" +
           "<td class='ta-num" + (bWins ? " ta-best" : "") + "'>" + vb + "</td></tr>";
  }

  function playerCol(name, p) {
    if (!p) return "<div class='ta-col ta-mut'>sem dados do TA para " + name + "</div>";
    var q = Object.keys(p.jogos.por_quadra).map(function (k) {
      var r = p.jogos.por_quadra[k];
      return k + " " + r.v + "–" + r.d;
    }).join(" · ");
    return (
      "<div class='ta-col'>" +
        "<h5><a class='ta-link' target='_blank' rel='noopener' href='" + p.ta_url + "'>" + name + "</a></h5>" +
        "<p><b>" + fmtRecord(p.jogos) + "</b> em " + p.ano +
          " <span class='ta-mut'>(" + q + ")</span></p>" +
        "<p>Forma: " + fmtForma(p.forma_ult10) + "</p>" +
        "<p class='ta-h'>Melhores vitórias</p><ul>" + fmtJogos(p.melhores_vitorias, "v.") + "</ul>" +
        "<p class='ta-h'>Piores derrotas</p><ul>" + fmtJogos(p.piores_derrotas, "p/") + "</ul>" +
      "</div>"
    );
  }

  function statsTable(n1, p1, n2, p2) {
    if (!p1 || !p2 || !p1.saque || !p2.saque) return "";
    var s1 = p1.saque, s2 = p2.saque, d1 = p1.devolucao, d2 = p2.devolucao;
    return (
      "<table class='ta-stats'>" +
        "<caption>Saque e devolução — " + p1.ano + "</caption>" +
        "<thead><tr><th class='ta-num'>" + n1 + "</th><th></th><th class='ta-num'>" + n2 + "</th></tr></thead>" +
        "<tbody>" +
          statRow("1º saque dentro", s1["1st_in_pct"], s2["1st_in_pct"]) +
          statRow("Pts no 1º saque", s1["1st_won_pct"], s2["1st_won_pct"]) +
          statRow("Pts no 2º saque", s1["2nd_won_pct"], s2["2nd_won_pct"]) +
          statRow("Pts de saque (total)", s1.spw_pct, s2.spw_pct) +
          statRow("Games de saque vencidos", s1.hold_pct, s2.hold_pct) +
          statRow("Aces", s1.aces_pct, s2.aces_pct) +
          statRow("Duplas faltas", s1.df_pct, s2.df_pct, true) +
          statRow("BPs salvos", s1.bp_salvos_pct, s2.bp_salvos_pct) +
          statRow("Pts de devolução", d1.rpw_pct, d2.rpw_pct) +
          statRow("Games de devolução quebrados", d1.brk_pct, d2.brk_pct) +
          statRow("BPs convertidos", d1.bp_convertidos_pct, d2.bp_convertidos_pct) +
        "</tbody>" +
      "</table>"
    );
  }

  function renderPanels(root) {
    (root || document).querySelectorAll(".ta-panel[data-match]").forEach(function (el) {
      if (el.dataset.taDone) return;
      el.dataset.taDone = "1";
      var names = el.dataset.match.split("|");
      var n1 = names[0].trim(), n2 = names[1].trim();
      var p1 = cache.players[n1], p2 = cache.players[n2];
      if (!p1 && !p2) return; // sem dados: painel não aparece
      el.innerHTML =
        "<details class='ta-details'>" +
          "<summary>Contexto dos jogadores <span class='ta-mut'>(Tennis Abstract)</span></summary>" +
          "<div class='ta-grid'>" + playerCol(n1, p1) + playerCol(n2, p2) + "</div>" +
          statsTable(n1, p1, n2, p2) +
        "</details>";
    });
  }

  /* ---- API --------------------------------------------------------------- */
  function taToks(n){
    var p = String(n).toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "")
      .replace(/[^a-z0-9 ]/g, " ").split(" ").filter(function (x) { return x.length > 1; });
    if (!p.length) return [];
    var longest = p.reduce(function (a, b) { return b.length > a.length ? b : a; });
    var out = [p[p.length - 1]];
    if (out.indexOf(longest) < 0) out.push(longest);
    return out;
  }
  function taDia(ymd){
    return Date.UTC(+String(ymd).slice(0, 4), +String(ymd).slice(4, 6) - 1, +String(ymd).slice(6, 8)) / 864e5;
  }
  function taPlacar(n1, n2, ymd){
    if (!cache || !cache.players || !ymd) return null;
    var alvoDia = taDia(ymd);
    var tentativas = [[n1, n2], [n2, n1]];
    for (var t = 0; t < 2; t++){
      var p = cache.players[tentativas[t][0]];
      if (!p || !p.jogos_recentes) continue;
      var alvo = taToks(tentativas[t][1]);
      for (var i = p.jogos_recentes.length - 1; i >= 0; i--){
        var m = p.jogos_recentes[i];                 // [data, adversario, placar, wl]
        if (Math.abs(taDia(m[0]) - alvoDia) > 1) continue;
        var to = taToks(m[1]);
        var casa = alvo.some(function (a) { return to.indexOf(a) >= 0; });
        if (casa && m[2] && m[2].indexOf("W/O") < 0) return m[2];
      }
    }
    return null;
  }

  window.TA = {
    placar: taPlacar,
    init: function (root) {
      if (cache) { linkNames(root); renderPanels(root); return; }
      fetch(DATA_URL, { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : { players: {} }; })
        .catch(function () { return { players: {} }; })
        .then(function (j) {
          cache = j; linkNames(root); renderPanels(root);
          document.dispatchEvent(new CustomEvent("ta-ready"));
        });
    },
    url: taUrl,
  };
})();
