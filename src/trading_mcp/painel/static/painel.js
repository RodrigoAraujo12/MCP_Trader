/* Painel MCP Trader: gráfico (Lightweight Charts) com a estrutura SMC desenhada e a leitura do servidor ao lado.
   Só lê a API local; nada aqui envia ordens. */
(() => {
  'use strict';

  const LW = window.LightweightCharts;
  const $ = (sel) => document.querySelector(sel);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const COR = {
    surface: css('--surface'), grid: css('--grid'), texto: css('--text'), texto2: css('--text-2'),
    sutil: css('--text-muted'), borda: 'rgba(255,255,255,0.13)', alta: css('--bull'), baixa: css('--bear'),
    liq: css('--liq'), destaque: css('--accent'),
  };
  const FONTE = '"Geist", system-ui, sans-serif';
  const FONTE_MONO = '"Geist Mono", ui-monospace, monospace';

  const TIMEFRAMES = ['M1', 'M3', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1'];
  const FUSOS = [
    { id: 'sp', rotulo: 'SP', tz: 'America/Sao_Paulo', nome: 'São Paulo' },
    { id: 'ny', rotulo: 'NY', tz: 'America/New_York', nome: 'Nova York' },
    { id: 'utc', rotulo: 'UTC', tz: 'UTC', nome: 'UTC' },
  ];
  const CAMADAS = [
    { id: 'ob', rotulo: 'Order blocks', amostra: `background:${alpha(COR.alta, 0.35)};box-shadow:inset 2px 0 0 ${COR.alta}` },
    { id: 'fvg', rotulo: 'FVG', amostra: `border:1px dashed ${COR.baixa};background:${alpha(COR.baixa, 0.12)}` },
    { id: 'estrutura', rotulo: 'BOS / CHoCH', amostra: `height:2px;background:${COR.alta}` },
    { id: 'topos', rotulo: 'HH / LL', amostra: `height:2px;background:${COR.texto2}` },
    { id: 'liquidez', rotulo: 'Liquidez', amostra: `height:0;border-top:2px dotted ${COR.liq}` },
    { id: 'niveis', rotulo: 'Dia e semana', amostra: `height:0;border-top:2px dashed ${COR.liq}` },
    { id: 'sessoes', rotulo: 'Sessões', amostra: `background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.2)` },
  ];
  const ATUALIZA_MS = 1000;
  const ESTRUTURA_MS = 60000;

  // ------------------------------------------------------------------ estado
  const url = new URL(location.href);
  const estado = {
    simbolo: (url.searchParams.get('simbolo') || 'USTEC').toUpperCase(),
    tf: TIMEFRAMES.includes((url.searchParams.get('tf') || '').toUpperCase()) ? url.searchParams.get('tf').toUpperCase() : 'M5',
    fuso: FUSOS.find((f) => f.id === url.searchParams.get('fuso')) || FUSOS[0],
    camadas: lerCamadas(),
    geracao: 0,
    grafico: null,       // resposta de /api/grafico
    estrutura: null,     // resposta de /api/estrutura
    velas: [],
    digitos: 2,
    ultimoPreco: null,
    fechaEm: null,
    diferencaRelogio: 0,
  };

  function lerCamadas() {
    const padrao = Object.fromEntries(CAMADAS.map((c) => [c.id, true]));
    try {
      return { ...padrao, ...JSON.parse(localStorage.getItem('painel.camadas') || '{}') };
    } catch {
      return padrao;
    }
  }
  function salvarCamadas() {
    try { localStorage.setItem('painel.camadas', JSON.stringify(estado.camadas)); } catch { /* sem armazenamento: só não lembra */ }
  }
  function atualizarUrl() {
    const u = new URL(location.href);
    u.searchParams.set('simbolo', estado.simbolo);
    u.searchParams.set('tf', estado.tf);
    u.searchParams.set('fuso', estado.fuso.id);
    history.replaceState(null, '', u);
  }

  // ------------------------------------------------------------------ formatação
  function alpha(hex, a) {
    const n = parseInt(hex.replace('#', ''), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }
  let fmtPreco = new Intl.NumberFormat('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtPct = new Intl.NumberFormat('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero' });
  const preco = (v) => (v == null ? '-' : fmtPreco.format(v));
  const pct = (v) => (v == null ? '' : `${fmtPct.format(v)}%`);
  const cacheData = new Map();
  function fmtData(opts) {
    const chave = estado.fuso.tz + JSON.stringify(opts);
    if (!cacheData.has(chave)) cacheData.set(chave, new Intl.DateTimeFormat('pt-BR', { timeZone: estado.fuso.tz, ...opts }));
    return cacheData.get(chave);
  }
  const hora = (ts) => fmtData({ hour: '2-digit', minute: '2-digit' }).format(new Date(ts * 1000));
  const horaIso = (iso) => (iso ? hora(Date.parse(iso) / 1000) : '');
  const dataHora = (ts) => fmtData({ day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(ts * 1000));
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ------------------------------------------------------------------ gráfico
  function marcaDeTempo(time, tipo) {
    const d = new Date(time * 1000);
    if (tipo === 0) return fmtData({ year: 'numeric' }).format(d);
    if (tipo === 1) return fmtData({ month: 'short' }).format(d).replace('.', '');
    if (tipo === 2) return fmtData({ day: '2-digit', month: '2-digit' }).format(d);
    return fmtData({ hour: '2-digit', minute: '2-digit' }).format(d);
  }
  function horaDoCursor(t) {
    return fmtData({ weekday: 'short', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
      .format(new Date(t * 1000)).replace('.', '');
  }
  // Funções novas a cada troca de fuso: é assim que o gráfico sabe que precisa refazer os rótulos de tempo.
  function formatadores() {
    return {
      timeScale: { tickMarkFormatter: (...a) => marcaDeTempo(...a) },
      localization: { locale: 'pt-BR', priceFormatter: (p) => fmtPreco.format(p), timeFormatter: (t) => horaDoCursor(t) },
    };
  }
  const grafico = LW.createChart($('#grafico'), {
    autoSize: true,
    layout: {
      background: { type: 'solid', color: COR.surface }, textColor: COR.sutil, fontFamily: FONTE_MONO, fontSize: 11,
      attributionLogo: true,
    },
    grid: { vertLines: { color: COR.grid }, horzLines: { color: COR.grid } },
    rightPriceScale: { borderColor: COR.borda, scaleMargins: { top: 0.08, bottom: 0.06 } },
    timeScale: {
      borderColor: COR.borda, timeVisible: true, secondsVisible: false, rightOffset: 14, barSpacing: 7, minBarSpacing: 1,
    },
    crosshair: {
      mode: LW.CrosshairMode.Normal,
      vertLine: { color: 'rgba(255,255,255,0.25)', labelBackgroundColor: '#2a2f36' },
      horzLine: { color: 'rgba(255,255,255,0.25)', labelBackgroundColor: '#2a2f36' },
    },
  });
  grafico.applyOptions(formatadores());
  const velas = grafico.addSeries(LW.CandlestickSeries, {
    upColor: COR.alta, downColor: COR.baixa, borderUpColor: COR.alta, borderDownColor: COR.baixa,
    wickUpColor: COR.alta, wickDownColor: COR.baixa, priceLineColor: 'rgba(255,255,255,0.35)', priceLineStyle: LW.LineStyle.Dotted,
  });

  // Primitiva que desenha caixas (embaixo dos candles) e linhas/rótulos (em cima) a partir dos índices dos candles.
  class CamadaSmc {
    constructor() {
      this.dados = null;
      this.vistas = [
        { zOrder: () => 'bottom', renderer: () => ({ draw: (alvo) => this.desenhar(alvo, 'fundo') }) },
        { zOrder: () => 'normal', renderer: () => ({ draw: (alvo) => this.desenhar(alvo, 'frente') }) },
      ];
    }
    attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.pedir = requestUpdate; }
    detached() { this.pedir = null; }
    paneViews() { return this.vistas; }
    atualizar(dados) { this.dados = dados; if (this.pedir) this.pedir(); }

    desenhar(alvo, camada) {
      const d = this.dados;
      if (!d || !this.chart) return;
      alvo.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        const escala = this.chart.timeScale();
        const x = (i) => escala.logicalToCoordinate(i);
        const y = (p) => this.series.priceToCoordinate(p);
        const x0 = x(0), x1 = x(1);
        if (x0 == null || x1 == null) return;
        const meio = Math.max((x1 - x0) / 2, 0.5);
        const largura = mediaSize.width;
        const ultimo = estado.velas.length - 1;
        const ok = (...v) => v.every((n) => n != null && Number.isFinite(n));
        const visivelX = (a, b) => b >= 0 && a <= largura;
        ctx.save();
        if (camada === 'fundo') {
          const rotulosFundo = [];
          this.rotulosSessao = [];  // vão para a fila da frente, que roda logo depois neste mesmo quadro
          if (estado.camadas.sessoes) {
            for (const s of d.sessoes) {
              const a = x(s.de_i) - meio, b = (s.em_andamento ? x(ultimo) : x(s.ate_i)) + meio;
              const t = y(s.maxima), f = y(s.minima);
              if (!ok(a, b, t, f) || !visivelX(a, b)) continue;
              ctx.fillStyle = 'rgba(255,255,255,0.028)';
              ctx.fillRect(a, t, b - a, f - t);
              ctx.strokeStyle = 'rgba(255,255,255,0.12)';
              ctx.lineWidth = 1;
              ctx.strokeRect(Math.round(a) + 0.5, Math.round(t) + 0.5, Math.round(b - a), Math.round(f - t));
              if (b - a > 44) this.rotulosSessao.push({ prioridade: 100, texto: s.rotulo, px: a + 4, py: t - 3, op: { cor: COR.sutil, base: 'bottom', fundo: false } });
            }
          }
          if (estado.camadas.fvg) {
            for (const z of d.smc.fvg) {
              const a = x(z.i) - meio, t = y(z.ate), f = y(z.de);
              if (!ok(a, t, f) || a > largura) continue;
              const cor = z.direcao === 'alta' ? COR.alta : COR.baixa;
              ctx.fillStyle = alpha(cor, 0.09);
              ctx.fillRect(a, t, largura - a, f - t);
              ctx.setLineDash([4, 3]);
              ctx.strokeStyle = alpha(cor, 0.75);
              ctx.lineWidth = 1;
              ctx.strokeRect(Math.round(a) + 0.5, Math.round(t) + 0.5, Math.round(largura - a) + 2, Math.round(f - t));
              ctx.setLineDash([]);
              const texto = z.preenchido_pct > 0 ? `FVG ${Math.round(z.preenchido_pct)}%` : 'FVG';
              if (f - t >= 11) rotulosFundo.push({ prioridade: 2, texto, px: Math.max(a, 0) + 5, py: (t + f) / 2, op: { cor, base: 'middle', fundo: false } });
            }
          }
          if (estado.camadas.ob) {
            for (const z of d.smc.order_blocks) {
              const a = x(z.i) - meio, t = y(z.ate), f = y(z.de);
              if (!ok(a, t, f) || a > largura) continue;
              const cor = z.direcao === 'alta' ? COR.alta : COR.baixa;
              ctx.fillStyle = alpha(cor, z.tocado ? 0.12 : 0.18);
              ctx.fillRect(a, t, largura - a, f - t);
              ctx.fillStyle = cor;
              ctx.fillRect(a, t, 2, f - t);
              const texto = z.estrutura === 'micro' ? 'OB' : z.estrutura === 'macro' ? 'OB macro' : 'OB micro e macro';
              if (f - t >= 11) rotulosFundo.push({ prioridade: 3, texto, px: Math.max(a, 0) + 6, py: (t + f) / 2, op: { cor, base: 'middle', fundo: false, peso: 600 } });
            }
          }
          const ocupados = [];
          for (const r of rotulosFundo.sort((p, q) => q.prioridade - p.prioridade)) rotulo(ctx, r.texto, r.px, r.py, { ...r.op, ocupados });
          ctx.restore();
          return;
        }

        // Linhas primeiro; os rótulos depois, por ordem de importância, pulando os que colidem.
        const rotulos = [];
        if (estado.camadas.estrutura) {
          for (const r of d.smc.rompimentos) {
            const a = x(r.de_i), b = x(r.ate_i), nivel = y(r.nivel);
            if (!ok(a, b, nivel) || !visivelX(a, b)) continue;
            const cor = r.direcao === 'alta' ? COR.alta : COR.baixa;
            const macro = r.estrutura === 'macro';
            ctx.strokeStyle = macro ? cor : alpha(cor, 0.8);
            ctx.lineWidth = macro ? 1.5 : 1;
            ctx.setLineDash(macro ? [] : [4, 3]);
            linha(ctx, a, nivel, b, nivel);
            ctx.setLineDash([]);
            // Com o gráfico muito afastado, os rótulos micro viram ruído: ficam só as linhas.
            if (b - a < 18 || (!macro && meio < 2)) continue;
            const acima = r.direcao === 'alta';
            rotulos.push({
              prioridade: (macro ? 40000 : 10000) + r.ate_i, texto: r.tipo, px: (a + b) / 2, py: nivel + (acima ? -3 : 3),
              op: { cor: macro ? cor : alpha(cor, 0.85), base: acima ? 'bottom' : 'top', alinhar: 'center', fundo: false,
                tamanho: macro ? 11 : 10, peso: r.tipo === 'CHoCH' ? 700 : 500 },
            });
          }
        }
        if (estado.camadas.liquidez) {
          for (const l of d.smc.liquidez) {
            const nivel = y(l.nivel);
            const a = x(l.pontos_i[0]);
            const ativo = l.varrido_i == null && l.rompido_i == null;
            const b = ativo ? largura : x(l.varrido_i);
            if (!ok(nivel, a, b) || !visivelX(a, b)) continue;
            ctx.globalAlpha = ativo ? 1 : 0.55;
            ctx.strokeStyle = COR.liq;
            ctx.lineWidth = 1.5;
            ctx.setLineDash([2, 3]);
            linha(ctx, a, nivel, b, nivel);
            ctx.setLineDash([]);
            ctx.fillStyle = COR.liq;
            for (const i of l.pontos_i) {
              const px = x(i);
              if (px != null) { ctx.beginPath(); ctx.arc(px, nivel, 2.5, 0, Math.PI * 2); ctx.fill(); }
            }
            ctx.globalAlpha = 1;
            rotulos.push({
              prioridade: ativo ? 30000 : 0, texto: ativo ? l.tipo : `${l.tipo} varrido`,
              px: ativo ? largura - 6 : b + 6, py: nivel + (l.tipo === 'EQH' ? -4 : 4),
              op: { cor: ativo ? COR.liq : alpha(COR.liq, 0.7), base: l.tipo === 'EQH' ? 'bottom' : 'top',
                alinhar: ativo ? 'right' : 'left', fundo: true },
            });
          }
        }
        if (estado.camadas.liquidez) {
          for (const v of d.smc.varreduras) {
            const c = estado.velas[v.i];
            const px = x(v.i);
            if (!c || !ok(px) || px < -10 || px > largura + 10) continue;
            const acima = v.lado === 'acima';
            const ponta = y(acima ? c.high : c.low);
            if (!ok(ponta)) continue;
            const py = ponta + (acima ? -5 : 5);
            ctx.fillStyle = COR.liq;
            ctx.beginPath();  // triângulo apontando para o pavio que varreu
            ctx.moveTo(px, py);
            ctx.lineTo(px - 4, py + (acima ? -6 : 6));
            ctx.lineTo(px + 4, py + (acima ? -6 : 6));
            ctx.closePath();
            ctx.fill();
            rotulos.push({
              prioridade: 25000 + v.i, texto: 'sweep', px, py: py + (acima ? -8 : 8),
              op: { cor: COR.liq, base: acima ? 'bottom' : 'top', alinhar: 'center', fundo: false, tamanho: 10 },
            });
          }
        }
        if (estado.camadas.topos) {
          for (const s of d.smc.topos_fundos) {
            const px = x(s.i), py = y(s.preco);
            if (!ok(px, py) || px < -20 || px > largura + 20) continue;
            const topo = s.tipo === 'topo';
            rotulos.push({
              prioridade: 20000 + s.i, texto: s.rotulo, px, py: py + (topo ? -8 : 8),
              op: { cor: COR.texto2, base: topo ? 'bottom' : 'top', alinhar: 'center', fundo: false, tamanho: 10, peso: 600 },
            });
          }
        }
        rotulos.push(...(this.rotulosSessao || []));
        const ocupados = [];
        for (const r of rotulos.sort((p, q) => q.prioridade - p.prioridade)) {
          rotulo(ctx, r.texto, r.px, r.py, { ...r.op, ocupados });
        }
        ctx.restore();
      });
    }
  }

  function linha(ctx, a, y0, b, y1) {
    const yy = Math.round(y0) + 0.5;
    ctx.beginPath();
    ctx.moveTo(a, yy);
    ctx.lineTo(b, Math.round(y1) + 0.5);
    ctx.stroke();
  }

  // Desenha um rótulo, a menos que ele caia em cima de outro já desenhado neste quadro (``ocupados``):
  // quem desenha antes tem prioridade, então a ordem de desenho é a ordem de importância.
  function rotulo(ctx, texto, px, py, { cor, base = 'middle', alinhar = 'left', fundo = true, tamanho = 10, peso = 500, ocupados = null }) {
    ctx.font = `${peso} ${tamanho}px ${FONTE}`;
    ctx.textBaseline = base;
    ctx.textAlign = alinhar;
    const w = ctx.measureText(texto).width + 8;
    const h = tamanho + 6;
    const left = alinhar === 'right' ? px - w + 4 : alinhar === 'center' ? px - w / 2 : px - 4;
    const top = base === 'bottom' ? py - h + 2 : base === 'top' ? py - 2 : py - h / 2;
    if (ocupados) {
      if (ocupados.some((r) => left < r.x + r.w && left + w > r.x && top < r.y + r.h && top + h > r.y)) return false;
      ocupados.push({ x: left, y: top, w, h });
    }
    if (fundo) {
      ctx.fillStyle = 'rgba(18,20,24,0.88)';
      ctx.beginPath();
      ctx.roundRect(left, top, w, h, 3);
      ctx.fill();
    }
    ctx.fillStyle = cor;
    ctx.fillText(texto, px, py);
    return true;
  }

  const camadaSmc = new CamadaSmc();
  velas.attachPrimitive(camadaSmc);

  // Linhas de preço: máximas/mínimas de dia e semana e topos/fundos diários (da estrutura), e o destaque dos alvos.
  let linhasNiveis = [];
  let linhaDestaque = null;
  function desenharNiveis() {
    for (const l of linhasNiveis) velas.removePriceLine(l);
    linhasNiveis = [];
    const n = estado.estrutura && estado.estrutura.niveis;
    if (!n || !estado.camadas.niveis) return;
    const itens = [];
    const periodo = (p, alto, baixo) => {
      if (!p) return;
      if (p.maxima != null) itens.push({ preco: p.maxima, titulo: alto, tomado: p.maxima_varrida_em || p.maxima_rompida_em });
      if (p.minima != null) itens.push({ preco: p.minima, titulo: baixo, tomado: p.minima_varrida_em || p.minima_rompida_em });
    };
    periodo(n.dia_mercado_anterior, 'PDH', 'PDL');
    periodo(n.semana_mercado_anterior, 'PWH', 'PWL');
    for (const s of n.topos_fundos_diarios || []) {
      itens.push({ preco: s.preco, titulo: `${s.tipo === 'topo' ? 'Topo' : 'Fundo'} D1 ${s.dia.slice(8, 10)}/${s.dia.slice(5, 7)}`, tomado: false });
    }
    for (const it of itens) {
      linhasNiveis.push(velas.createPriceLine({
        price: it.preco, color: it.tomado ? alpha(COR.liq, 0.4) : COR.liq, lineWidth: 1,
        lineStyle: it.tomado ? LW.LineStyle.SparseDotted : LW.LineStyle.Dashed,
        axisLabelVisible: !it.tomado, title: it.tomado ? `${it.titulo} tomada` : it.titulo,
        axisLabelColor: COR.liq, axisLabelTextColor: '#121418',
      }));
    }
  }
  function destacar(precoAlvo, titulo) {
    limparDestaque();
    linhaDestaque = velas.createPriceLine({
      price: precoAlvo, color: COR.destaque, lineWidth: 2, lineStyle: LW.LineStyle.Solid, axisLabelVisible: true, title: titulo,
    });
  }
  function limparDestaque() {
    if (linhaDestaque) { velas.removePriceLine(linhaDestaque); linhaDestaque = null; }
  }

  // Legenda OHLC: o candle sob o cursor, ou o último.
  function legenda(c) {
    const d = estado.grafico;
    if (!d || !c) { $('#legenda').innerHTML = ''; return; }
    const variacao = c.open ? ((c.close / c.open) - 1) * 100 : 0;
    const classe = c.close >= c.open ? 'alta' : 'baixa';
    $('#legenda').innerHTML =
      `<b>${esc(d.simbolo)}</b><span class="rot">${esc(d.timeframe)}</span><span class="rot">${esc(dataHora(c.time))}</span>` +
      `<span><span class="rot">A</span> ${preco(c.open)}</span><span><span class="rot">M</span> ${preco(c.high)}</span>` +
      `<span><span class="rot">m</span> ${preco(c.low)}</span><span><span class="rot">F</span> ${preco(c.close)}</span>` +
      `<span class="${classe}">${pct(variacao)}</span>`;
  }
  grafico.subscribeCrosshairMove((param) => {
    const c = param && param.time != null ? param.seriesData.get(velas) : null;
    legenda(c || estado.velas[estado.velas.length - 1]);
  });

  // ------------------------------------------------------------------ API
  async function api(caminho, params) {
    const u = new URL(caminho, location.origin);
    for (const [k, v] of Object.entries(params || {})) u.searchParams.set(k, v);
    const r = await fetch(u, { cache: 'no-store' });
    const corpo = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(corpo.erro || `Erro ${r.status}`), { status: r.status });
    return corpo;
  }

  // ------------------------------------------------------------------ carregamento do gráfico
  let recarregarTimer = null;
  async function carregarGrafico({ manterVista = false } = {}) {
    const geracao = estado.geracao;
    if (!manterVista) {
      $('#carregando').hidden = false;
      $('#erro-grafico').hidden = true;
    }
    try {
      const d = await api('/api/grafico', { simbolo: estado.simbolo, tf: estado.tf });
      if (geracao !== estado.geracao) return;
      const vista = manterVista ? grafico.timeScale().getVisibleLogicalRange() : null;
      estado.grafico = d;
      estado.velas = d.candles.slice();
      if (d.digitos !== estado.digitos) {
        estado.digitos = d.digitos;
        fmtPreco = new Intl.NumberFormat('pt-BR', { minimumFractionDigits: d.digitos, maximumFractionDigits: d.digitos });
        velas.applyOptions({ priceFormat: { type: 'price', precision: d.digitos, minMove: 10 ** -d.digitos } });
        grafico.applyOptions(formatadores());
      }
      velas.setData(estado.velas);
      camadaSmc.atualizar(d);
      if (vista) grafico.timeScale().setVisibleLogicalRange(vista);
      else grafico.timeScale().setVisibleLogicalRange({ from: estado.velas.length - 160, to: estado.velas.length + 12 });
      legenda(estado.velas[estado.velas.length - 1]);
      $('#grafico').setAttribute('aria-label',
        `Gráfico de ${d.simbolo} em ${d.timeframe}: estrutura micro ${d.smc.tendencia.micro}, macro ${d.smc.tendencia.macro}.`);
      document.title = `${d.simbolo} ${d.timeframe} · Painel MCP Trader`;
      $('#carregando').hidden = true;
      $('#erro-simbolo').hidden = true;
      avisos();
    } catch (e) {
      if (geracao !== estado.geracao) return;
      if (manterVista) return; // falha numa recarga de fundo: o gráfico atual continua valendo
      $('#carregando').hidden = true;
      $('#erro-grafico').hidden = false;
      $('#erro-grafico-texto').textContent = e.status === 404
        ? `${e.message} Confira o nome (ex.: USTEC, XAUUSD, EURUSD).`
        : `Não foi possível carregar o gráfico: ${e.message} Confira se o MetaTrader 5 está aberto e conectado.`;
      if (e.status === 404) mostrarErroSimbolo(e.message);
    }
  }
  function agendarRecarga() {
    clearTimeout(recarregarTimer);
    // Espera o terminal fechar o candle antes de recalcular a estrutura.
    recarregarTimer = setTimeout(() => { carregarGrafico({ manterVista: true }); carregarEstrutura(); }, 1500);
  }

  // ------------------------------------------------------------------ ao vivo
  let vivoTimer = null;
  async function aoVivo() {
    clearTimeout(vivoTimer);
    const geracao = estado.geracao;
    if (!document.hidden) {
      try {
        const d = await api('/api/ao_vivo', { simbolo: estado.simbolo, tf: estado.tf });
        if (geracao === estado.geracao) aplicarAoVivo(d);
      } catch (e) {
        if (geracao === estado.geracao) marcarEstado('erro', 'Sem cotação', e.message);
      }
    }
    vivoTimer = setTimeout(aoVivo, ATUALIZA_MS);
  }

  function aplicarAoVivo(d) {
    const q = d.cotacao;
    estado.diferencaRelogio = d.agora - Date.now() / 1000;
    estado.fechaEm = d.fecha_em;
    // Preço
    const el = $('#preco');
    if (estado.ultimoPreco != null && q.bid !== estado.ultimoPreco) {
      el.classList.remove('sobe', 'desce');
      void el.offsetWidth;
      el.classList.add(q.bid > estado.ultimoPreco ? 'sobe' : 'desce');
      clearTimeout(el._t);
      el._t = setTimeout(() => el.classList.remove('sobe', 'desce'), 700);
    }
    estado.ultimoPreco = q.bid;
    el.textContent = preco(q.bid);
    $('#spread').textContent = `spread ${preco(q.spread_preco)}`;
    const textos = {
      atual: 'Ao vivo', atrasado: `Atrasado ${idade(q.idade_s)}`, mercado_fechado_provavel: 'Mercado fechado',
      horario_inconsistente: 'Horário inconsistente',
    };
    marcarEstado(q.estado, textos[q.estado] || q.estado, q.aviso);
    // Candles: atualiza o último e acrescenta o novo, sem recarregar o gráfico.
    if (!estado.grafico || !estado.velas.length) return;
    let novo = false;
    for (const c of d.candles) {
      const ultimo = estado.velas[estado.velas.length - 1];
      if (c.time < ultimo.time) continue;
      if (c.time === ultimo.time) estado.velas[estado.velas.length - 1] = c;
      else { estado.velas.push(c); novo = true; }
      velas.update(c);
    }
    if (novo) agendarRecarga();
    legenda(estado.velas[estado.velas.length - 1]);
  }

  function idade(s) {
    if (s < 90) return `${Math.round(s)} s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    if (s < 172800) return `${Math.floor(s / 3600)} h ${String(Math.round((s % 3600) / 60)).padStart(2, '0')} min`;
    return `${Math.round(s / 86400)} dias`;
  }

  function marcarEstado(chave, texto, dica) {
    const el = $('#estado');
    el.dataset.estado = chave;
    $('#estado-texto').textContent = texto;
    el.title = dica || '';
  }

  setInterval(() => {
    const el = $('#contagem');
    if (!estado.fechaEm) { el.textContent = ''; return; }
    const falta = Math.max(0, Math.round(estado.fechaEm - (Date.now() / 1000 + estado.diferencaRelogio)));
    const h = Math.floor(falta / 3600), m = Math.floor((falta % 3600) / 60), s = falta % 60;
    el.textContent = `fecha em ${h ? `${h}:${String(m).padStart(2, '0')}` : m}:${String(s).padStart(2, '0')}`;
  }, 250);

  // ------------------------------------------------------------------ barra lateral (estrutura)
  let estruturaTimer = null;
  async function carregarEstrutura() {
    clearTimeout(estruturaTimer);
    const geracao = estado.geracao;
    try {
      const d = await api('/api/estrutura', { simbolo: estado.simbolo, tf: estado.tf });
      if (geracao !== estado.geracao) return;
      estado.estrutura = d;
      renderEstrutura(d);
      renderFaixa(d);
      renderAlvos(d);
      renderSessoes(d);
      renderDiaSemana(d);
      desenharNiveis();
      avisos();
      $('#atualizado').textContent = `Estrutura de ${hora(Date.parse(d.coletado.utc) / 1000)} (${estado.fuso.nome})`;
    } catch (e) {
      if (geracao !== estado.geracao) return;
      $('#atualizado').textContent = `Estrutura indisponível: ${e.message}`;
      // Sem dados novos, nada do símbolo anterior pode continuar na tela.
      if (!estado.estrutura) limparLateral(`Indisponível: ${e.message}`);
    }
    estruturaTimer = setTimeout(carregarEstrutura, ESTRUTURA_MS);
  }

  function limparLateral(texto) {
    const p = `<p class="sutil">${esc(texto)}</p>`;
    $('#tabela-estrutura tbody').innerHTML = `<tr><td colspan="3" class="sutil">${esc(texto)}</td></tr>`;
    $('#nota-pavio').hidden = true;
    $('#faixa-tf').textContent = estado.tf;
    $('#faixa').innerHTML = p;
    $('#alvos').innerHTML = `<li class="vazio">${esc(texto)}</li>`;
    $('#sessoes').innerHTML = p;
    $('#dia-semana').innerHTML = p;
    $('#secao-avisos').hidden = true;
  }

  const seta = { alta: '▲', baixa: '▼', indefinida: '•' };
  function tend(valor) {
    const v = valor || 'indefinida';
    return `<span class="tend ${v}"><span aria-hidden="true">${seta[v]}</span>${v === 'indefinida' ? 'indefinida' : v}</span>`;
  }

  function renderEstrutura(d) {
    const ordem = ['H4', 'H1', 'M30', 'M15', 'M5', 'M3', 'M1', 'D1'].filter((tf) => d.timeframes[tf]);
    ordem.sort((a, b) => TIMEFRAMES.indexOf(b) - TIMEFRAMES.indexOf(a));
    const divergentes = [];
    const linhas = ordem.map((tf) => {
      const f = d.timeframes[tf];
      if (f.erro) return `<tr><td>${tf}</td><td colspan="2" class="sutil">${esc(f.erro)}</td></tr>`;
      const celula = (nome) => {
        const e = f.estrutura[nome];
        const diverge = e.tendencia_por_pavio !== e.tendencia_por_fechamento;
        if (diverge) divergentes.push(`${tf} ${nome}`);
        return `<td>${tend(e.tendencia_por_fechamento)}${diverge ? `<span class="diverge" title="Por pavio: ${esc(e.tendencia_por_pavio)}">pavio: ${esc(e.tendencia_por_pavio)}</span>` : ''}</td>`;
      };
      return `<tr class="${tf === estado.tf ? 'atual' : ''}"><td>${tf}</td>${celula('micro')}${celula('macro')}</tr>`;
    });
    $('#tabela-estrutura tbody').innerHTML = linhas.join('') || '<tr><td colspan="3" class="sutil">Sem dados.</td></tr>';
    const nota = $('#nota-pavio');
    nota.hidden = !divergentes.length;
    nota.textContent = divergentes.length ? `Leitura por pavio diverge em: ${divergentes.join(', ')}.` : '';
  }

  function renderFaixa(d) {
    const f = d.timeframes[estado.tf];
    $('#faixa-tf').textContent = estado.tf;
    const pd = f && f.premium_discount;
    if (!pd) { $('#faixa').innerHTML = '<p class="sutil">Sem topo e fundo macro definidos neste timeframe.</p>'; return; }
    const pos = Math.max(0, Math.min(100, pd.posicao_pct));
    const nomes = { premium: 'Premium', discount: 'Discount', equilibrio: 'Equilíbrio', acima_da_faixa: 'Acima da faixa', abaixo_da_faixa: 'Abaixo da faixa' };
    $('#faixa').innerHTML =
      `<div class="trilho" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pos}" aria-label="Posição na faixa macro">` +
      `<div class="marcador" style="left:${pos}%"></div></div>` +
      `<div class="rotulos"><span>Discount</span><span>Equilíbrio</span><span>Premium</span></div>` +
      `<p class="resumo"><b>${nomes[pd.zona] || esc(pd.zona)}</b> em ${fmtPct.format(pd.posicao_pct).replace('+', '')}% da faixa ` +
      `<span class="num">${preco(pd.faixa[0])}</span> a <span class="num">${preco(pd.faixa[1])}</span></p>`;
  }

  function renderAlvos(d) {
    const a = d.alvos;
    const ol = $('#alvos');
    if (!a) { ol.innerHTML = '<li class="vazio">Sem cotação para calcular os alvos.</li>'; return; }
    const item = (x) => `<li class="alvo" tabindex="0" data-preco="${x.preco}" data-titulo="${esc(x.tipos[0])}">` +
      `<span class="p">${preco(x.preco)}</span><span class="d">${pct(x.distancia_pct)}</span>` +
      `<span class="t">${x.tipos.map(esc).join(', ')}</span></li>`;
    const acima = a.acima.slice().reverse().map(item).join('') || '<li class="vazio">Nada mapeado acima.</li>';
    const abaixo = a.abaixo.map(item).join('') || '<li class="vazio">Nada mapeado abaixo.</li>';
    ol.innerHTML = acima + `<li class="agora"><span>Preço de referência</span><span class="p num">${preco(a.referencia)}</span></li>` + abaixo;
  }
  for (const evento of ['mouseover', 'focusin']) {
    $('#alvos').addEventListener(evento, (e) => {
      const li = e.target.closest('li.alvo');
      if (li) destacar(Number(li.dataset.preco), li.dataset.titulo);
    });
  }
  for (const evento of ['mouseleave', 'focusout']) $('#alvos').addEventListener(evento, limparDestaque);

  const SESSOES = [['asia', 'Ásia'], ['londres', 'Londres'], ['nova_york', 'Nova York']];
  function tomada(p, lado) {
    if (p[`${lado}_rompida_em`]) return `<small class="tomada">rompida ${horaIso(p[`${lado}_rompida_em`])}</small>`;
    if (p[`${lado}_varrida_em`]) return `<small class="tomada">varrida ${horaIso(p[`${lado}_varrida_em`])}</small>`;
    return '';
  }
  function renderSessoes(d) {
    const s = d.niveis && d.niveis.sessoes_hoje;
    if (!s) { $('#sessoes').innerHTML = '<p class="sutil">Indisponível.</p>'; return; }
    const situacao = { ainda_nao_comecou: 'ainda não começou', em_andamento: 'em andamento', concluida: 'concluída', sem_negociacao: 'sem negociação' };
    const linhas = SESSOES.map(([chave, nome]) => {
      const p = s[chave] || {};
      const periodo = p.de ? `${horaIso(p.de)} a ${horaIso(p.ate)}, ${situacao[p.situacao] || ''}` : '';
      return `<span class="nome">${nome}</span><span class="v">${preco(p.maxima)}${tomada(p, 'maxima')}</span>` +
        `<span class="v">${preco(p.minima)}${tomada(p, 'minima')}</span><span class="situacao">${esc(periodo)}</span>`;
    });
    $('#sessoes').innerHTML = `<div class="linhas"><span class="cab"></span><span class="cab">Máxima</span><span class="cab">Mínima</span>${linhas.join('')}</div>`;
  }
  function renderDiaSemana(d) {
    const n = d.niveis;
    if (!n) { $('#dia-semana').innerHTML = '<p class="sutil">Indisponível.</p>'; return; }
    const linha = (nome, p) => (p
      ? `<span class="nome">${nome}</span><span class="v">${preco(p.maxima)}${tomada(p, 'maxima')}</span><span class="v">${preco(p.minima)}${tomada(p, 'minima')}</span>`
      : '');
    $('#dia-semana').innerHTML = `<div class="linhas"><span class="cab"></span><span class="cab">Máxima</span><span class="cab">Mínima</span>` +
      linha('Hoje', n.dia_mercado_atual) + linha('Dia anterior', n.dia_mercado_anterior) +
      linha('Semana', n.semana_mercado_atual) + linha('Semana anterior', n.semana_mercado_anterior) + '</div>' +
      '<p class="nota" style="margin:10px 0 0">Dia de mercado vira às 17:00 de Nova York. PDH/PDL e PWH/PWL ficam no gráfico.</p>';
  }

  function avisos() {
    const lista = [...((estado.grafico && estado.grafico.avisos) || []), ...((estado.estrutura && estado.estrutura.avisos) || [])];
    const unicos = [...new Set(lista)];
    $('#secao-avisos').hidden = !unicos.length;
    $('#avisos').innerHTML = unicos.map((a) => `<li>${esc(a)}</li>`).join('');
  }

  function mostrarErroSimbolo(msg) {
    const el = $('#erro-simbolo');
    el.textContent = msg;
    el.hidden = false;
  }

  // ------------------------------------------------------------------ controles
  function montarTimeframes() {
    $('#timeframes').innerHTML = TIMEFRAMES.map((tf, i) =>
      `<button type="button" data-tf="${tf}" aria-pressed="${tf === estado.tf}" title="${tf} (tecla ${i + 1})">${tf}</button>`).join('');
  }
  function montarFusos() {
    $('#fusos').innerHTML = FUSOS.map((f) =>
      `<button type="button" data-fuso="${f.id}" aria-pressed="${f.id === estado.fuso.id}" title="Horários de ${f.nome}">${f.rotulo}</button>`).join('');
  }
  function montarCamadas() {
    $('#camadas').innerHTML = CAMADAS.map((c) =>
      `<button type="button" data-camada="${c.id}" aria-pressed="${!!estado.camadas[c.id]}">` +
      `<span class="amostra" style="${c.amostra}" aria-hidden="true"></span>${c.rotulo}</button>`).join('');
  }

  function trocar({ simbolo, tf } = {}) {
    if (simbolo) estado.simbolo = simbolo.toUpperCase();
    if (tf) estado.tf = tf;
    estado.geracao += 1;
    estado.ultimoPreco = null;
    estado.fechaEm = null;
    estado.estrutura = null;
    estado.grafico = null;
    estado.velas = [];
    $('#campo-simbolo').value = estado.simbolo;
    $('#preco').textContent = '-';
    $('#spread').textContent = '';
    $('#legenda').innerHTML = '';
    $('#atualizado').textContent = 'Carregando…';
    document.title = 'Painel MCP Trader';
    marcarEstado('', 'Conectando…');
    limparLateral('Carregando…');
    montarTimeframes();
    atualizarUrl();
    camadaSmc.atualizar(null);
    desenharNiveis();
    carregarGrafico();
    carregarEstrutura();
    aoVivo();
  }

  $('#timeframes').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-tf]');
    if (b && b.dataset.tf !== estado.tf) trocar({ tf: b.dataset.tf });
  });
  $('#fusos').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-fuso]');
    if (!b) return;
    estado.fuso = FUSOS.find((f) => f.id === b.dataset.fuso);
    cacheData.clear();
    montarFusos();
    atualizarUrl();
    grafico.applyOptions(formatadores());
    if (estado.estrutura) { renderSessoes(estado.estrutura); renderDiaSemana(estado.estrutura); }
    legenda(estado.velas[estado.velas.length - 1]);
  });
  $('#camadas').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-camada]');
    if (!b) return;
    estado.camadas[b.dataset.camada] = !estado.camadas[b.dataset.camada];
    salvarCamadas();
    montarCamadas();
    camadaSmc.atualizar(estado.grafico);
    desenharNiveis();
  });
  $('#form-simbolo').addEventListener('submit', (e) => {
    e.preventDefault();
    const valor = $('#campo-simbolo').value.trim();
    if (!valor) { mostrarErroSimbolo('Digite um símbolo, por exemplo USTEC.'); return; }
    $('#erro-simbolo').hidden = true;
    if (valor.toUpperCase() !== estado.simbolo) trocar({ simbolo: valor });
  });
  $('#campo-simbolo').addEventListener('change', () => $('#form-simbolo').requestSubmit());
  $('#tentar-de-novo').addEventListener('click', () => trocar());
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || e.target.closest('input, textarea, select')) return;
    const n = Number(e.key);
    if (n >= 1 && n <= TIMEFRAMES.length && TIMEFRAMES[n - 1] !== estado.tf) trocar({ tf: TIMEFRAMES[n - 1] });
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { aoVivo(); carregarGrafico({ manterVista: true }); } });

  // ------------------------------------------------------------------ início
  (async () => {
    montarFusos();
    montarCamadas();
    try {
      const cfg = await api('/api/config');
      $('#lista-simbolos').innerHTML = cfg.instrumentos.map((s) => `<option value="${esc(s)}"></option>`).join('');
    } catch { /* sem a lista, o campo continua aceitando qualquer símbolo */ }
    trocar();
  })();
})();
