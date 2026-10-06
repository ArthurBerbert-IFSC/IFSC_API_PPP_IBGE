#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PPP EM SESSÕES CURTAS - IBGE-PPP x coordenada oficial da RBMC (2026)

Para as mesmas 10 estações da RBMC do artigo do COBRAC 2024, o script:
  1. baixa o RINEX diário (15 s) de alguns dias de 2026;
  2. corta sessões de 1, 2, 3 e 4 h começando às 08h (manhã) e às 13h (tarde),
     em hora solar local, e mantém o arquivo de 24 h como referência;
  3. envia cada sessão ao IBGE-PPP pela API oficial e baixa o resultado;
  4. compara a coordenada SIRGAS2000 (época 2000.4) de cada sessão com a
     coordenada oficial da estação e com a solução de 24 h do mesmo dia;
  5. gera tabelas (CSV e XLSX), gráficos (PNG) e um arquivo com os números
     do resumo.

Uso (Windows, Linux ou macOS; Python 3.9+):
  pip install -r requirements.txt
  python ppp_gui.py                      # programa com janela (recomendado)
  python ppp_sessoes.py tudo --email seu@email   # ou pela linha de comando

Etapas separadas (todas podem ser repetidas; o que já foi feito é pulado):
  python ppp_sessoes.py baixar            # RINEX da RBMC + relatórios das estações
  python ppp_sessoes.py cortar            # sessões de 1-4 h manhã/tarde + 24 h
  python ppp_sessoes.py testar-api --email seu@email   # envia 1 sessão e mostra a resposta
  python ppp_sessoes.py enviar --email seu@email       # envia todas e baixa os resultados
  python ppp_sessoes.py analisar          # tabelas, gráficos e números do resumo

Atenção: o IBGE-PPP manda um e-mail por processamento (3 dias x 10 estações
x 9 sessões = até 270 e-mails). Use um endereço secundário ou crie um filtro.
"""

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import threading
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

# ============================== CONFIGURAÇÃO ==============================

ESTACOES = ["BOAV", "BRAZ", "CEFE", "CRUZ", "MTSR", "POAL", "POLI", "RNNA", "ROSA", "SALU"]

# Dias de 2026 (>= 18 dias atrás, para o IBGE-PPP usar órbita final).
# Se a estação não tiver dado no dia, o script tenta os 3 dias seguintes.
DIAS = ["2026-08-12", "2026-08-26", "2026-09-09"]

DURACOES_H = [1, 2, 3, 4]          # durações das sessões curtas
INICIO_MANHA_HSL = 8.0             # início da sessão da manhã (hora solar local)
INICIO_TARDE_HSL = 13.0            # início da sessão da tarde (hora solar local)
TOLERANCIA_PLANI_MM = 80.0         # NBR 17047:2022, vértice de imóvel urbano
PAUSA_ENTRE_ENVIOS_S = 5           # intervalo entre um processamento e o próximo
BAIXAR_ZIP = False                 # o link do ZIP fica atrás de um desafio do Cloudflare;
                                   # as coordenadas vêm no resumo da própria resposta da API
COBERTURA_MINIMA = 0.8             # fração mínima de épocas para aceitar a sessão

BASE = Path(os.environ.get("PPP_BASE", Path(__file__).resolve().parent))  # PPP_BASE: usado nos testes
PASTA = BASE / "dados"
SAIDA = BASE / "saida"

# URLs (podem ser trocadas por variáveis de ambiente, usado só nos testes)
URL_RINEX2 = os.environ.get(
    "URL_RINEX2",
    "https://geoftp.ibge.gov.br/informacoes_sobre_posicionamento_geodesico/rbmc/dados/{ano}/{doy:03d}/{sta}{doy:03d}1.zip")
URL_RINEX3 = os.environ.get(
    "URL_RINEX3", "https://servicodados.ibge.gov.br/api/v1/rbmc/rinex3/{sta}/{ano}/{doy}")
URLS_RELATORIO = os.environ.get(
    "URLS_RELATORIO",
    "https://servicodados.ibge.gov.br/api/v1/rbmc/relatorio/{sta}|"
    "https://geoftp.ibge.gov.br/informacoes_sobre_posicionamento_geodesico/rbmc/relatorio/{STA}.pdf").split("|")
URL_PPP = os.environ.get("URL_PPP", "https://servicodados.ibge.gov.br/api/v1/ppp")

# ============================== UTILITÁRIOS ==============================

A_GRS80 = 6378137.0
F_GRS80 = 1 / 298.257222101
E2_GRS80 = F_GRS80 * (2 - F_GRS80)


PARAR = threading.Event()   # a interface liga isto para interromper com segurança
_ouvintes = []              # funções f(tipo, dados) avisadas de log, progresso e esperas


class Interrompido(Exception):
    """Levantada quando o usuário pede para parar."""


def ouvir(funcao):
    _ouvintes.append(funcao)


def _avisar(tipo, dados):
    for f in list(_ouvintes):
        try:
            f(tipo, dados)
        except Exception:  # noqa
            pass


def checar_parada():
    if PARAR.is_set():
        raise Interrompido()


def progresso(etapa, feito, total, detalhe=""):
    _avisar("progresso", {"etapa": etapa, "feito": feito, "total": total, "detalhe": detalhe})


def esperar(segundos, motivo=""):
    """Espera em passos de 1 s, avisando a contagem regressiva e respeitando o botão Parar."""
    for restante in range(int(round(segundos)), 0, -1):
        checar_parada()
        _avisar("espera", {"motivo": motivo, "restante": restante})
        time.sleep(1)
    _avisar("espera", {"motivo": "", "restante": 0})


def log(msg):
    try:
        print(msg, flush=True)
    except (UnicodeEncodeError, OSError, ValueError, AttributeError):
        pass  # sem console (pythonw) ou console sem UTF-8
    _avisar("log", msg)
    try:  # cópia em dados/execucao.log, para acompanhar o andamento
        PASTA.mkdir(parents=True, exist_ok=True)
        with open(PASTA / "execucao.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except OSError:
        pass


def num_br(s):
    """'26,5528' -> 26.5528 ; '1.106,020' -> 1106.02 ; '8.63' -> 8.63"""
    s = s.strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def primeiro_numero(s):
    """Primeiro número do texto; aceita milhar com ponto ('1.106,02 m' -> 1106.02)."""
    m = re.search(r"-?\d{1,3}(?:\.\d{3})+(?:,\d+)?|-?\d+(?:[.,]\d+)?", s or "")
    return num_br(m.group(0)) if m else None


def dms_para_graus(txt):
    """Aceita '-30 04 26.5528', '30° 04\\' 26,5528" S', '-30.07404' etc."""
    if txt is None:
        return None
    t = txt.replace("º", "°").strip()
    hemi = re.search(r"([NSEWLO])\s*$", t)
    neg = t.startswith("-") or (hemi is not None and hemi.group(1) in "SWO")
    vals = [num_br(n) for n in re.findall(r"\d+(?:[.,]\d+)?", t)]
    if not vals:
        return None
    if len(vals) >= 3:
        g = vals[0] + vals[1] / 60 + vals[2] / 3600
    elif len(vals) == 2:
        g = vals[0] + vals[1] / 60
    else:
        g = vals[0]
    return -g if neg else g


def xyz_para_geo(x, y, z):
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - E2_GRS80))
    for _ in range(10):
        n = A_GRS80 / math.sqrt(1 - E2_GRS80 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - E2_GRS80 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon), h


def diferenca_enu_mm(lat, lon, h, lat_ref, lon_ref, h_ref):
    """Diferença (N, E, h) em mm de (lat, lon, h) em relação à referência, GRS80."""
    phi = math.radians(lat_ref)
    s2 = math.sin(phi) ** 2
    m = A_GRS80 * (1 - E2_GRS80) / (1 - E2_GRS80 * s2) ** 1.5
    n = A_GRS80 / math.sqrt(1 - E2_GRS80 * s2)
    dn = math.radians(lat - lat_ref) * (m + h_ref) * 1000
    de = math.radians(lon - lon_ref) * (n + h_ref) * math.cos(phi) * 1000
    dh = (h - h_ref) * 1000
    return dn, de, dh


def ano_decimal(dt):
    ini = datetime(dt.year, 1, 1)
    fim = datetime(dt.year + 1, 1, 1)
    return dt.year + (dt - ini).total_seconds() / (fim - ini).total_seconds()


def sessao(session_http=[]):
    if not session_http:
        import requests
        s = requests.Session()
        session_http.append(s)
    return session_http[0]


def http_get(url, timeout=120, headers=None):
    r = sessao().get(url, timeout=timeout, headers=headers)
    return r.status_code, r.content, r.headers.get("Content-Type", "")


def ler_csv(caminho):
    if not caminho.exists():
        return []
    with open(caminho, newline="", encoding="utf-8-sig", errors="replace") as f:
        primeira = f.readline()
        f.seek(0)
        sep = ";" if primeira.count(";") > primeira.count(",") else ","  # Excel em português salva com ';'
        return list(csv.DictReader(f, delimiter=sep))


def gravar_csv(caminho, linhas, campos=None):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    if not linhas:
        return
    campos = campos or list(linhas[0].keys())
    with open(caminho, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(linhas)

# ============================== RINEX ==============================


def ler_rinex(texto):
    """Separa cabeçalho e corpo e extrai as informações necessárias."""
    linhas = texto.splitlines()
    fim = next(i for i, l in enumerate(linhas) if l[60:73] == "END OF HEADER")
    cab, corpo = linhas[:fim + 1], linhas[fim + 1:]
    info = {"versao": float(cab[0][:9]), "ntipos": 0, "xyz": None, "antena": None,
            "alt_antena": 0.0, "marcador": ""}
    tipos = []
    for l in cab:
        rot = l[60:].strip()
        if rot == "# / TYPES OF OBSERV":
            if l[:6].strip():
                info["ntipos"] = int(l[:6])
            tipos += l[6:60].split()
        elif rot == "APPROX POSITION XYZ":
            info["xyz"] = tuple(float(v) for v in l[:42].split()[:3])
        elif rot == "ANT # / TYPE":
            modelo, radome = l[20:36].strip(), l[36:40].strip() or "NONE"
            info["antena"] = f"{modelo} {radome}"
        elif rot == "ANTENNA: DELTA H/E/N":
            info["alt_antena"] = float(l[:14])
        elif rot == "MARKER NAME":
            info["marcador"] = l[:60].strip()
    info["tipos"] = tipos
    return cab, corpo, info


def _tempo_rinex2(l):
    if not l[1:26].strip():
        return None
    yy = int(l[1:3])
    base = datetime(2000 + yy if yy < 80 else 1900 + yy, int(l[4:6]), int(l[7:9]),
                    int(l[10:12]), int(l[13:15]))
    return base + timedelta(seconds=float(l[15:26]))


def _tempo_rinex3(l):
    if not l[2:29].strip():
        return None
    base = datetime(int(l[2:6]), int(l[7:9]), int(l[10:12]), int(l[13:15]), int(l[16:18]))
    return base + timedelta(seconds=float(l[18:29]))


def blocos_rinex2(corpo, ntipos):
    """Gera (tempo, flag, linhas) para cada registro de época do RINEX 2.xx."""
    lps = max(1, math.ceil(ntipos / 5))
    i, n = 0, len(corpo)
    while i < n:
        l = corpo[i]
        if not l.strip():
            i += 1
            continue
        flag = int(l[28:29] or 0) if l[28:29].strip() else 0
        nsat = int(l[29:32]) if l[29:32].strip() else 0
        if flag in (0, 1, 6):
            tam = 1 + (math.ceil(nsat / 12) - 1 if nsat else 0) + nsat * lps
        else:
            tam = 1 + nsat
        yield _tempo_rinex2(l), flag, corpo[i:i + tam]
        i += tam


def blocos_rinex3(corpo):
    i, n = 0, len(corpo)
    while i < n:
        l = corpo[i]
        if not l.startswith(">"):
            i += 1
            continue
        flag = int(l[31]) if len(l) > 31 and l[31].strip() else 0
        nsat = int(l[32:35]) if l[32:35].strip() else 0
        tam = 1 + nsat
        yield _tempo_rinex3(l), flag, corpo[i:i + tam]
        i += tam


def _linha_tempo_cab(dt, sistema, rotulo):
    seg = dt.second + dt.microsecond / 1e6
    corpo = f"{dt.year:6d}{dt.month:6d}{dt.day:6d}{dt.hour:6d}{dt.minute:6d}{seg:13.7f}     {sistema:<3s}"
    return f"{corpo:<60s}{rotulo}"


def cortar_rinex(texto, t0, t1, comentario=""):
    """Devolve (texto_cortado, n_epocas, primeira, ultima) com as épocas t0 <= t < t1."""
    cab, corpo, info = ler_rinex(texto)
    v3 = info["versao"] >= 3
    gerador = blocos_rinex3(corpo) if v3 else blocos_rinex2(corpo, info["ntipos"])
    saida, n, prim, ult, dentro = [], 0, None, None, False
    for t, flag, bloco in gerador:
        if t is not None:
            dentro = t0 <= t < t1
            if t >= t1 and flag in (0, 1, 6):
                break
        if dentro:
            saida.extend(bloco)
            if flag in (0, 1) and t is not None:
                n += 1
                prim = prim or t
                ult = t
    novo_cab = []
    for l in cab:
        rot = l[60:].strip()
        if rot in ("TIME OF LAST OBS", "# OF SATELLITES", "PRN / # OF OBS"):
            continue
        if rot == "TIME OF FIRST OBS" and prim:
            l = _linha_tempo_cab(prim, l[48:51].strip() or "GPS", "TIME OF FIRST OBS")
        if rot == "END OF HEADER" and comentario:
            novo_cab.append(f"{comentario[:60]:<60s}COMMENT")
        novo_cab.append(l)
    return "\n".join(novo_cab + saida) + "\n", n, prim, ult


def intervalo_s(texto):
    for l in texto.splitlines()[:200]:
        if l[60:].strip() == "INTERVAL":
            return float(l[:10])
        if l[60:73] == "END OF HEADER":
            break
    return 15.0

# ============================== ETAPA 1: BAIXAR ==============================


def baixar_rinex(sta, dia):
    """Tenta RINEX 2 (geoftp) e depois RINEX 3 (API). Devolve (bytes, versao, url) ou None."""
    import hatanaka
    ano, doy = dia.year, dia.timetuple().tm_yday
    url = URL_RINEX2.format(ano=ano, doy=doy, sta=sta.lower(), STA=sta)
    try:
        st, conteudo, _ = http_get(url)
        if not (st == 200 and conteudo[:2] == b"PK"):
            log(f"    RINEX 2 indisponível (HTTP {st})")
        if st == 200 and conteudo[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
                obs = [n for n in z.namelist() if re.search(r"\.\d\d[doDO](\.(gz|Z))?$", n)]
                if obs:
                    txt = hatanaka.decompress(z.read(obs[0]))
                    if b"RINEX VERSION" in txt[:400]:
                        return txt, 2, url
    except Exception as e:  # noqa
        log(f"    RINEX 2 falhou: {e}")
    url = URL_RINEX3.format(ano=ano, doy=doy, sta=sta.lower(), STA=sta)
    try:
        st, conteudo, _ = http_get(url)
        if not (st == 200 and len(conteudo) > 1000):
            log(f"    RINEX 3 indisponível (HTTP {st}): {conteudo[:120]!r}")
        if st == 200 and len(conteudo) > 1000:
            if conteudo[:2] == b"PK":
                with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
                    obs = [n for n in z.namelist() if re.search(r"MO\.(crx|rnx)", n, re.I)] or z.namelist()
                    conteudo = z.read(obs[0])
            txt = hatanaka.decompress(conteudo)
            if b"RINEX VERSION" in txt[:400]:
                return txt, 3, url
    except Exception as e:  # noqa
        log(f"    RINEX 3 falhou: {e}")
    return None


def etapa_baixar(args):
    pasta = PASTA / "rinex"
    pasta.mkdir(parents=True, exist_ok=True)
    manifesto = {(r["estacao"], r["dia_alvo"]): r for r in ler_csv(PASTA / "rinex.csv")}
    hoje = date.today()
    total, feito = len(args.dias) * len(args.estacoes), 0
    for dia_txt in args.dias:
        dia_alvo = date.fromisoformat(dia_txt)
        if (hoje - dia_alvo).days < 18:
            log(f"AVISO: {dia_txt} tem menos de 18 dias; o IBGE-PPP pode usar órbita rápida.")
        for sta in args.estacoes:
            checar_parada()
            feito += 1
            progresso("Baixando RINEX da RBMC", feito, total, f"{sta} {dia_txt}")
            chave = (sta, dia_txt)
            if chave in manifesto and Path(manifesto[chave]["arquivo"]).exists():
                continue
            log(f"Baixando {sta} {dia_txt} ...")
            obtido = None
            for k in range(4):
                dia = dia_alvo + timedelta(days=k)
                obtido = baixar_rinex(sta, dia)
                if obtido:
                    break
                log(f"    sem dados em {dia}; tentando o dia seguinte")
            if not obtido:
                log(f"    ERRO: {sta} sem dados de {dia_alvo} a {dia_alvo + timedelta(days=3)}")
                continue
            conteudo, versao, url = obtido
            doy = dia.timetuple().tm_yday
            arq = pasta / f"{sta.lower()}{doy:03d}0.{dia.year % 100:02d}o"
            arq.write_bytes(conteudo)
            manifesto[chave] = {"estacao": sta, "dia_alvo": dia_txt, "dia": dia.isoformat(),
                                "versao_rinex": versao, "arquivo": str(arq), "url": url}
            log(f"    ok: {arq.name} (RINEX {versao}, {len(conteudo) / 1e6:.1f} MB)")
            gravar_csv(PASTA / "rinex.csv", list(manifesto.values()),
                       ["estacao", "dia_alvo", "dia", "versao_rinex", "arquivo", "url"])
    gravar_csv(PASTA / "rinex.csv", list(manifesto.values()),
               ["estacao", "dia_alvo", "dia", "versao_rinex", "arquivo", "url"])
    obter_coordenadas_oficiais(args.estacoes)

# ------------------------- coordenadas oficiais -------------------------


def extrair_coord_relatorio(texto):
    """Extrai lat/lon/h SIRGAS2000 e o código SAT do relatório da estação (tolerante a formatos).
    Formato atual do IBGE: 'Latitude: - 30º  04' 26,55276"' e 'Alt. Elip.: 76,745 m'."""
    t = texto.replace("º", "°").replace("’", "'").replace("”", '"').replace("''", '"')
    i = t.upper().find("SIRGAS")
    trecho = t[i:i + 3000] if i >= 0 else t
    dms = r"([+-]?)\s*(\d{1,3})\s*°\s*(\d{1,2})\s*'\s*(\d{1,2}(?:[.,]\d+)?)\s*\"?\s*([NSLOEW](?![a-z]))?"
    lat = re.search(r"Latitude\s*:?\s*" + dms, trecho)
    lon = re.search(r"Longitude\s*:?\s*" + dms, trecho)
    alt = re.search(r"Alt(?:itude)?\.?\s*(?:Elip(?:s[oó]idal)?\.?|Geom[ée]trica)[^0-9\-]{0,15}(-?[\d.,]+)",
                    trecho, re.I)
    sat = re.search(r"C[óo]digo SAT\s*:?\s*(\d+)", t)
    if not (lat and lon and alt):
        return None

    def conv(m):
        g = int(m.group(2)) + int(m.group(3)) / 60 + num_br(m.group(4)) / 3600
        neg = m.group(1) == "-" or (m.group(5) or "") in ("S", "O", "W")
        return -g if neg else g
    return conv(lat), conv(lon), num_br(alt.group(1)), (sat.group(1) if sat else "")


def obter_coordenadas_oficiais(estacoes):
    """Usa dados/coordenadas_oficiais.csv se existir; senão tenta extrair dos relatórios."""
    arq = PASTA / "coordenadas_oficiais.csv"
    existentes = {r["estacao"]: r for r in ler_csv(arq)}
    pasta = PASTA / "relatorios"
    pasta.mkdir(parents=True, exist_ok=True)
    for sta in estacoes:
        if sta in existentes and existentes[sta].get("lat"):
            continue
        pdf = pasta / f"{sta}.pdf"
        if not pdf.exists():
            for modelo in URLS_RELATORIO:
                try:
                    st, conteudo, _ = http_get(modelo.format(sta=sta.lower(), STA=sta))
                    if st == 200 and conteudo[:4] == b"%PDF":
                        pdf.write_bytes(conteudo)
                        break
                except Exception:  # noqa
                    pass
        coord = None
        if pdf.exists():
            try:
                from pypdf import PdfReader
                texto = "\n".join(p.extract_text() or "" for p in PdfReader(str(pdf)).pages)
                coord = extrair_coord_relatorio(texto)
            except Exception as e:  # noqa
                log(f"    {sta}: não consegui ler o relatório ({e})")
        existentes[sta] = {"estacao": sta, "sat": coord[3] if coord else "",
                           "lat": f"{coord[0]:.10f}" if coord else "",
                           "lon": f"{coord[1]:.10f}" if coord else "",
                           "h": f"{coord[2]:.4f}" if coord else "",
                           "fonte": f"relatorio {pdf.name} (extraido automaticamente - CONFERIR)" if coord
                           else "PREENCHER A MAO (graus decimais ou 'gg mm ss,sss S')"}
    gravar_csv(arq, [existentes[s] for s in estacoes], ["estacao", "sat", "lat", "lon", "h", "fonte"])
    log(f"\nCoordenadas oficiais em {arq}:")
    for s in estacoes:
        r = existentes[s]
        log(f"  {s}: SAT {r.get('sat') or '?'}  lat={r['lat'] or '?'} lon={r['lon'] or '?'} h={r['h'] or '?'}  [{r['fonte']}]")
    log("CONFIRA esses valores com o relatório da estação (SIRGAS2000, época 2000.4) antes de analisar.")

# ============================== ETAPA 2: CORTAR ==============================


def janelas(dia, lon_graus, duracoes):
    """Lista (periodo, dur_h, letra, t0, t1) em UTC; início em hora solar local."""
    base = datetime(dia.year, dia.month, dia.day)
    fuso = lon_graus / 15.0
    res = []
    letras = "abcdefghijklmnopqrstuvwx"
    for k, periodo in enumerate(("manha", "tarde")):
        hsl = INICIO_MANHA_HSL if periodo == "manha" else INICIO_TARDE_HSL
        t0 = base + timedelta(hours=hsl - fuso)
        t0 = t0.replace(second=0, microsecond=0) + timedelta(minutes=1 if t0.second >= 30 else 0)
        for j, d in enumerate(duracoes):
            res.append((periodo, d, letras[k * len(duracoes) + j], t0, t0 + timedelta(hours=d)))
    res.append(("24h", 24, "0", base, base + timedelta(days=1)))
    return res


def etapa_cortar(args):
    pasta = PASTA / "sessoes"
    pasta.mkdir(parents=True, exist_ok=True)
    sessoes = {r["sessao"]: r for r in ler_csv(PASTA / "sessoes.csv")}
    arquivos = [r for r in ler_csv(PASTA / "rinex.csv")
                if r["estacao"] in args.estacoes and r["dia_alvo"] in args.dias]
    for k, r in enumerate(arquivos, 1):
        checar_parada()
        progresso("Cortando as sessões", k, len(arquivos), f"{r['estacao']} {r['dia']}")
        texto = Path(r["arquivo"]).read_text(encoding="latin-1")
        _, _, info = ler_rinex(texto)
        if not info["xyz"]:
            log(f"{r['estacao']}: sem APPROX POSITION XYZ no cabeçalho; pulando")
            continue
        lat, lon, _ = xyz_para_geo(*info["xyz"])
        passo = intervalo_s(texto)
        dia = date.fromisoformat(r["dia"])
        doy = dia.timetuple().tm_yday
        for periodo, dur, letra, t0, t1 in janelas(dia, lon, args.duracoes):
            nome = f"{r['estacao'].lower()}{doy:03d}{letra}.{dia.year % 100:02d}o"
            if nome in sessoes and (pasta / nome).exists():
                continue
            if periodo == "24h":
                corte, n, prim, ult = cortar_rinex(texto, t0, t1)
            else:
                com = f"ppp_sessoes: {periodo} {dur}h {t0:%H:%M}-{t1:%H:%M} UTC"
                corte, n, prim, ult = cortar_rinex(texto, t0, t1, com)
            esperado = (t1 - t0).total_seconds() / passo
            cobertura = n / esperado if esperado else 0
            (pasta / nome).write_text(corte, encoding="latin-1")
            sessoes[nome] = {
                "sessao": nome, "estacao": r["estacao"], "dia": r["dia"], "dia_alvo": r["dia_alvo"],
                "periodo": periodo, "dur_h": dur, "inicio_utc": t0.isoformat(), "fim_utc": t1.isoformat(),
                "hsl_inicio": INICIO_MANHA_HSL if periodo == "manha" else (INICIO_TARDE_HSL if periodo == "tarde" else 0),
                "n_epocas": n, "cobertura": f"{cobertura:.3f}",
                "antena": info["antena"] or "", "alt_antena": info["alt_antena"],
                "lat_aprox": f"{lat:.6f}", "lon_aprox": f"{lon:.6f}",
                "valida": "sim" if cobertura >= COBERTURA_MINIMA else "nao"}
            log(f"  {nome}: {periodo:5s} {dur:>2}h  {t0:%H:%M}-{t1:%H:%M} UTC  "
                f"{n} épocas ({cobertura:.0%}){'' if cobertura >= COBERTURA_MINIMA else '  <- LACUNA, não será enviada'}")
    campos = ["sessao", "estacao", "dia", "dia_alvo", "periodo", "dur_h", "inicio_utc", "fim_utc",
              "hsl_inicio", "n_epocas", "cobertura", "antena", "alt_antena", "lat_aprox", "lon_aprox", "valida"]
    gravar_csv(PASTA / "sessoes.csv", sorted(sessoes.values(), key=lambda x: x["sessao"]), campos)
    log(f"{len(sessoes)} sessões em {PASTA / 'sessoes.csv'}")

# ============================== ETAPA 3: ENVIAR ==============================


def achar_urls(obj):
    achadas = []
    if isinstance(obj, dict):
        for v in obj.values():
            achadas += achar_urls(v)
    elif isinstance(obj, list):
        for v in obj:
            achadas += achar_urls(v)
    elif isinstance(obj, str):
        achadas += re.findall(r"https?://[^\s\"'<>]+", obj)
    return achadas


def tentar_baixar_resultado(pasta, urls, tentativas=1, espera=30, mostrar=False):
    """Baixa o ZIP de resultado a partir das URLs da resposta. True se conseguiu."""
    urls = sorted(set(urls), key=lambda u: (".zip" not in u.lower(), u))
    for k in range(tentativas):
        for u in urls:
            try:
                st, conteudo, tipo = http_get(u, timeout=180, headers=cabecalhos_api())
            except Exception as e:  # noqa
                if mostrar:
                    log(f"    download do ZIP falhou: {e}")
                continue
            if mostrar and conteudo[:2] != b"PK":
                log(f"    ZIP ainda indisponível (HTTP {st}, {tipo}): {conteudo[:150]!r}")
            if st == 200 and conteudo[:2] == b"PK":
                (pasta / "resultado.zip").write_bytes(conteudo)
                return True
            if st == 200 and b"<html" in conteudo[:2000].lower():
                links = re.findall(rb'href="([^"]+\.zip)"', conteudo, re.I)
                for lk in links:
                    lk = lk.decode()
                    if not lk.startswith("http"):
                        from urllib.parse import urljoin
                        lk = urljoin(u, lk)
                    st2, c2, _ = http_get(lk, timeout=180, headers=cabecalhos_api())
                    if st2 == 200 and c2[:2] == b"PK":
                        (pasta / "resultado.zip").write_bytes(c2)
                        return True
        if k < tentativas - 1:
            esperar(espera, "aguardando o resultado do IBGE-PPP")
    return False


UA_CURL = "curl/8.9.1"  # o manual do IBGE-PPP documenta o uso da API com o curl
# Formas de envio, testadas em ordem; a primeira aceita fica guardada em
# dados/api_variante.json e passa a ser usada direto nas outras sessões.
VARIANTES_API = [
    {"nome": "crx_curl", "arquivo": "crx", "ua": UA_CURL, "marco_vazio": False},
    {"nome": "crx_curl_marco_vazio", "arquivo": "crx", "ua": UA_CURL, "marco_vazio": True},
    {"nome": "zip_curl", "arquivo": "zip", "ua": UA_CURL, "marco_vazio": False},
    {"nome": "obs_curl", "arquivo": "obs", "ua": UA_CURL, "marco_vazio": False},
    {"nome": "crx_python", "arquivo": "crx", "ua": None, "marco_vazio": False},
]


def variante_salva():
    arq = PASTA / "api_variante.json"
    if arq.exists():
        nome = json.loads(arq.read_text(encoding="utf-8")).get("nome")
        for v in VARIANTES_API:
            if v["nome"] == nome:
                return v
    return None


def cabecalhos_api(v=None):
    v = v or variante_salva() or VARIANTES_API[0]
    h = {"Accept": "application/json, */*"}
    if v["ua"]:
        h["User-Agent"] = v["ua"]
    else:  # identificação padrão da biblioteca requests
        import requests
        h["User-Agent"] = requests.utils.default_user_agent()
    return h


def rejeitado_pelo_firewall(texto):
    t = str(texto)
    return "Request Rejected" in t or "requested URL was rejected" in t


def arquivo_para_envio(nome_sessao, tipo):
    """Devolve (nome_do_arquivo, bytes) conforme o tipo: crx (Hatanaka .yyd), zip ou obs."""
    import hatanaka
    bruto = (PASTA / "sessoes" / nome_sessao).read_bytes()
    base, ext = nome_sessao.rsplit(".", 1)
    if tipo == "crx":
        crx = hatanaka.compress(bruto, compression="none")
        if len(crx) <= 9.5e6:
            return f"{base}.{ext[:2]}d", crx
        buf = io.BytesIO()  # o servidor derruba envios acima de ~10 MB: manda o Hatanaka dentro de um ZIP
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{base}.{ext[:2]}d", crx)
        return f"{base}.zip", buf.getvalue()
    if tipo == "zip":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(nome_sessao, bruto)
        return f"{base}.zip", buf.getvalue()
    return nome_sessao, bruto


FORMATOS_ANTENA = ["igs", "rinex", "modelo"]  # como informar a antena; testados em ordem


def valor_antena(antena, formato):
    """'TRM115000.00 NONE' -> 'TRM115000.00    NONE' (igs), None (usa o cabeçalho RINEX) ou 'TRM115000.00'."""
    partes = antena.split()
    modelo = partes[0] if partes else ""
    radome = partes[1] if len(partes) > 1 else "NONE"
    if formato == "igs":
        return f"{modelo:<16s}{radome}"
    if formato == "modelo":
        return modelo
    return None


def antena_reconhecida(resumo):
    a = str(resumo.get("ANTENA", "")).upper()
    return bool(a) and "DISPON" not in a and "ENCONTRAD" not in a


def salvar_variante(nome, antena):
    (PASTA / "api_variante.json").write_text(json.dumps({"nome": nome, "antena": antena}), encoding="utf-8")


def enviar_sessao(s, email, mostrar=False, aguardar=True):
    """Envia uma sessão ao IBGE-PPP. Devolve 'ok', 'pendente' ou 'erro'."""
    import requests
    pasta = PASTA / "ppp" / s["sessao"]
    pasta.mkdir(parents=True, exist_ok=True)
    if (pasta / "resultado.zip").exists() or (pasta / "resumo.json").exists():
        return "ok"
    resp_arq = pasta / "resposta.json"
    if resp_arq.exists():
        resp = json.loads(resp_arq.read_text(encoding="utf-8"))
        resumo_antigo = resp.get("resposta", {}).get("resumo") if isinstance(resp.get("resposta"), dict) else None
        if rejeitado_pelo_firewall(resp.get("resposta")) or (resumo_antigo and not antena_reconhecida(resumo_antigo)):
            resp_arq.rename(pasta / f"descartada_{datetime.now():%H%M%S}.json")  # refazer
        elif resp.get("status_http") == 200 or resp.get("urls"):
            return "ok" if tentar_baixar_resultado(pasta, resp.get("urls", [])) else "pendente"
    salva = json.loads((PASTA / "api_variante.json").read_text(encoding="utf-8")) \
        if (PASTA / "api_variante.json").exists() else {}
    formas = [v for v in VARIANTES_API if v["nome"] == salva.get("nome")] or VARIANTES_API
    formatos = ([salva["antena"]] if salva.get("antena") else []) + \
        [f for f in FORMATOS_ANTENA if f != salva.get("antena")]
    r = v = None
    for fmt in formatos:
        # 1) acha uma forma de envio que o firewall aceite
        for v in formas:
            nome_arq, conteudo = arquivo_para_envio(s["sessao"], v["arquivo"])
            if len(conteudo) > 19.5e6:
                continue  # limite do IBGE-PPP: 20 MB
            dados = {"tipo-levantamento": "estatico", "altura-antena": f"{float(s['alt_antena']):.4f}",
                     "email": email, "autorizacao-uso": "false"}
            ant = valor_antena(s["antena"], fmt)
            if ant:
                dados["modelo-antena"] = ant
            if v["marco_vazio"]:
                dados["marco-ibge"] = ""
            try:
                r = sessao().post(URL_PPP, data=dados, headers=cabecalhos_api(v), timeout=900,
                                  files={"arquivo": (nome_arq, conteudo, "application/octet-stream")})
            except requests.RequestException as e:
                log(f"    falha de conexão: {e}")
                return "erro"
            if rejeitado_pelo_firewall(r.text[:3000]):
                sid = re.search(r"support ID is:\s*(\d+)", r.text)
                log(f"    forma de envio '{v['nome']}' barrada pelo firewall do IBGE"
                    f"{' (support ID ' + sid.group(1) + ')' if sid else ''}")
                r = None
                continue
            formas = [v]
            break
        if r is None:
            (pasta / "erro.json").write_text(json.dumps(
                {"erro": "todas as formas de envio foram barradas pelo firewall do IBGE"}), encoding="utf-8")
            log("    ERRO: todas as formas de envio foram barradas pelo firewall do IBGE")
            return "erro"
        try:
            corpo = r.json()
        except ValueError:
            corpo = r.text[:20000]
        resumo = corpo.get("resumo") if isinstance(corpo, dict) and isinstance(corpo.get("resumo"), dict) else None
        if mostrar:
            log(f"HTTP {r.status_code}  {r.headers.get('Content-Type', '')}  (antena: {fmt})")
            log(json.dumps(corpo, ensure_ascii=False, indent=1)[:3000] if not isinstance(corpo, str) else corpo[:3000])
        if r.status_code in (400, 422) and fmt != formatos[-1]:
            log(f"    IBGE-PPP recusou o envio com a antena no formato '{fmt}' (HTTP {r.status_code}); "
                "tentando o próximo formato")
            continue
        if resumo and resumo.get("LAT") and not antena_reconhecida(resumo):
            log(f"    antena não reconhecida pelo IBGE-PPP no formato '{fmt}' "
                f"({valor_antena(s['antena'], fmt)!r}); tentando o próximo formato")
            (pasta / f"antena_{fmt}_nao_reconhecida.json").write_text(
                json.dumps(corpo, ensure_ascii=False, indent=1), encoding="utf-8")
            continue
        break
    registro = {"status_http": r.status_code, "content_type": r.headers.get("Content-Type", ""),
                "enviado_em": datetime.now().isoformat(timespec="seconds"), "forma_envio": v["nome"],
                "formato_antena": fmt, "arquivo_enviado": nome_arq, "campos": {**dados, "email": "***"}}
    if r.content[:2] == b"PK":
        (pasta / "resultado.zip").write_bytes(r.content)
        resp_arq.write_text(json.dumps(registro, ensure_ascii=False, indent=1), encoding="utf-8")
        return "ok"
    registro["resposta"] = corpo
    registro["urls"] = achar_urls(corpo)
    if r.status_code != 200:
        (pasta / "erro.json").write_text(json.dumps(registro, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"    HTTP {r.status_code}: {str(corpo)[:300]}")
        return "erro"
    resp_arq.write_text(json.dumps(registro, ensure_ascii=False, indent=1), encoding="utf-8")
    if resumo and resumo.get("LAT"):
        if not antena_reconhecida(resumo):
            (pasta / "erro.json").write_text(json.dumps({"erro": "antena não reconhecida pelo IBGE-PPP",
                                                         "antena": s["antena"]}, ensure_ascii=False), encoding="utf-8")
            resp_arq.rename(pasta / f"descartada_{datetime.now():%H%M%S}.json")
            log(f"    ERRO: antena {s['antena']} não reconhecida pelo IBGE-PPP em nenhum formato")
            return "erro"
        if salva.get("antena") != fmt or salva.get("nome") != v["nome"]:
            salvar_variante(v["nome"], fmt)
            log(f"    envio aceito com a forma '{v['nome']}' e antena no formato '{fmt}' (vale para as próximas)")
        (pasta / "resumo.json").write_text(json.dumps(resumo, ensure_ascii=False, indent=1), encoding="utf-8")
        if BAIXAR_ZIP:  # o ZIP (com o .sum) é opcional
            tentar_baixar_resultado(pasta, registro["urls"], tentativas=1, mostrar=mostrar)
        return "ok"
    n = 6 if aguardar else 1
    return "ok" if tentar_baixar_resultado(pasta, registro["urls"], tentativas=n, espera=20) else "pendente"


def exigir_email(args):
    if not args.email or "@" not in args.email:
        sys.exit("Informe o e-mail que o IBGE-PPP exige: --email voce@dominio")


def etapa_testar_api(args):
    exigir_email(args)
    sess = [s for s in ler_csv(PASTA / "sessoes.csv") if s["valida"] == "sim" and s["periodo"] == "manha"]
    if not sess:
        sys.exit("Rode antes: python ppp_sessoes.py baixar  e  python ppp_sessoes.py cortar")
    s = sorted(sess, key=lambda x: float(x["dur_h"]))[0]
    log(f"Enviando {s['sessao']} ({s['estacao']}, {s['periodo']} {s['dur_h']} h) para {URL_PPP}")
    progresso("Testando a API do IBGE-PPP", 0, 1, s["sessao"])
    st = enviar_sessao(s, args.email, mostrar=True)
    progresso("Testando a API do IBGE-PPP", 1, 1, s["sessao"])
    log(f"\nSituação: {st}. Arquivos em {PASTA / 'ppp' / s['sessao']}")
    res = ler_resultado(PASTA / "ppp" / s["sessao"]) if st == "ok" else None
    if res and res.get("lat") is not None:
        log(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        log("\nTESTE OK: o IBGE-PPP processou a sessão e as coordenadas foram lidas.")
        return res
    elif st == "pendente":
        log("\nO IBGE-PPP aceitou o envio, mas o resultado ainda não ficou pronto. "
            "Rode de novo em alguns minutos; ele não reenvia, só busca o resultado.")
        sys.exit(2)
    else:
        log("\nTESTE FALHOU. Avise o Claude; os detalhes estão em dados/execucao.log.")
        sys.exit(1)


def etapa_enviar(args):
    exigir_email(args)
    sess = [s for s in ler_csv(PASTA / "sessoes.csv")
            if s["valida"] == "sim" and s["estacao"] in args.estacoes and s["dia_alvo"] in args.dias]
    # 24 h primeiro: se só der tempo de parte, ao menos a referência existe
    sess.sort(key=lambda s: (s["periodo"] != "24h", s["dia_alvo"], s["estacao"], s["sessao"]))
    situacao = {}
    inicio, n_novas = time.time(), 0
    for k, s in enumerate(sess, 1):
        checar_parada()
        pasta = PASTA / "ppp" / s["sessao"]
        ja_enviada = any((pasta / f).exists() for f in ("resultado.zip", "resumo.json", "resposta.json"))
        progresso("Enviando ao IBGE-PPP", k - 1, len(sess),
                  f"{s['sessao']} ({s['estacao']}, {s['periodo']} {s['dur_h']} h)")
        situacao[s["sessao"]] = enviar_sessao(s, args.email, aguardar=False)
        _avisar("contagem", {st: sum(1 for v in situacao.values() if v == st) for st in ("ok", "pendente", "erro")})
        if not ja_enviada:
            n_novas += 1
        media = (time.time() - inicio) / max(n_novas, 1)
        faltam = sum(1 for x in sess[k:] if not (PASTA / "ppp" / x["sessao"] / "resumo.json").exists())
        eta = time.strftime("%H:%M:%S", time.gmtime(media * faltam)) if n_novas else "--"
        log(f"[{k}/{len(sess)}] {s['sessao']} {s['estacao']} {s['periodo']:5s} {s['dur_h']:>2}h "
            f"-> {situacao[s['sessao']]}   (faltam ~{eta})")
        progresso("Enviando ao IBGE-PPP", k, len(sess), f"{s['sessao']} -> {situacao[s['sessao']]}")
        if k >= 3 and all(v == "erro" for v in situacao.values()):
            sys.exit("As 3 primeiras sessões deram erro; parei para não insistir no servidor do IBGE. "
                     "Os detalhes estão em dados/execucao.log.")
        if not ja_enviada and k < len(sess):
            esperar(PAUSA_ENTRE_ENVIOS_S, "intervalo entre envios")
    # depois de enviar tudo, busca os resultados que ainda estavam em processamento
    for rodada in range(30):
        pend = [s for s in sess if situacao[s["sessao"]] == "pendente"]
        if not pend:
            break
        log(f"{len(pend)} resultados em processamento; nova busca em 60 s (rodada {rodada + 1}/30)")
        esperar(60, f"{len(pend)} resultados ainda em processamento no IBGE")
        for s in pend:
            situacao[s["sessao"]] = enviar_sessao(s, args.email, aguardar=False)
    cont = {st: sum(1 for v in situacao.values() if v == st) for st in ("ok", "pendente", "erro")}
    _avisar("contagem", cont)
    log(f"\nConcluídas: {cont['ok']}  pendentes: {cont['pendente']}  com erro: {cont['erro']}")
    if cont["pendente"]:
        log("Ainda há resultados em processamento no IBGE. Rode 'enviar' de novo mais tarde: "
            "ele só busca os pendentes, não reenvia.")
    if cont["erro"]:
        log("Sessões com erro têm o detalhe em dados/ppp/<sessao>/erro.json. Rodar 'enviar' de novo "
            "tenta reenviar só essas.")

# ============================== LEITURA DOS RESULTADOS ==============================


def ler_txt_ppp(texto):
    d = {}
    for l in texto.splitlines():
        m = re.match(r"\s*([A-Z]{2,7})\s*(?:->|:|=)?\s*(.*?)\s*$", l)
        if m and m.group(2) and m.group(1) not in d:
            d[m.group(1)] = m.group(2)
    if "LAT" not in d or "LON" not in d:
        return None
    return {"lat": dms_para_graus(d["LAT"]), "lon": dms_para_graus(d["LON"]),
            "h": primeiro_numero(d.get("HGEO")), "slat": primeiro_numero(d.get("SLAT")),
            "slon": primeiro_numero(d.get("SLON")), "sh": primeiro_numero(d.get("SHGEO")),
            "orbita": d.get("ORBITA", ""), "freq": d.get("FREQ", ""), "observ": d.get("OBSERV", ""),
            "datum": d.get("DATUM", "")}


def ler_sum_ppp(texto):
    res = {}
    gms = r"(-?\d+)\s+(\d+)\s+(\d+(?:\.\d+)?)"
    for rot, chave in (("Latitude", "lat_ep"), ("Longitude", "lon_ep")):
        m = re.search(rot + r"\s*\(gms\)\s+" + gms, texto)
        if m:
            g = abs(int(m.group(1))) + int(m.group(2)) / 60 + float(m.group(3)) / 3600
            res[chave] = -g if m.group(1).startswith("-") else g
    m = re.search(r"Alt\.?\s*Geo\.?\s*\(m\)\s+(-?\d+(?:\.\d+)?)", texto)
    if m:
        res["h_ep"] = float(m.group(1))
    m = re.search(r"Inicio\s*:\s*(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)", texto)
    if m:
        res["inicio"] = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
    for sistema in ("GPS", "GLONASS"):
        m = re.search(r"observacoes processadas\s*:\s*(\d+)\s*" + sistema, texto)
        if m:
            res["obs_" + sistema.lower()] = int(m.group(1))
    m = re.search(r"Orbitas dos satelites\s+(\S+)", texto)
    if m:
        res["arq_orbita"] = m.group(1)
    m = re.search(r"ITRF \((\w+)\)->SIRGAS2000", texto)
    if m:
        res["referencial"] = m.group(1)
    return res


def ler_resultado(pasta):
    z = pasta / "resultado.zip"
    if not z.exists():
        rj = pasta / "resumo.json"
        if not rj.exists():
            return None
        resumo = json.loads(rj.read_text(encoding="utf-8"))
        res = ler_txt_ppp("\n".join(f"{k} -> {v}" for k, v in resumo.items() if k))
        if res:
            res["fonte"] = "resumo da API"
        return res
    with zipfile.ZipFile(z) as zf:
        nomes = zf.namelist()

        def ler(pred):
            for n in nomes:
                if pred(n.lower()):
                    return zf.read(n).decode("latin-1", errors="replace")
            return None
        txt = ler(lambda n: n.endswith(".txt") and "leiame" not in n and "readme" not in n)
        smm = ler(lambda n: n.endswith(".sum"))
    res = ler_txt_ppp(txt) if txt else None
    if not res:
        return None
    if smm:
        res.update(ler_sum_ppp(smm))
        if res.get("h_ep") is not None:
            res["h"] = res["h_ep"]  # o .sum traz a altitude com 4 casas (o .txt, com 2); é a mesma nas duas épocas
    res["fonte"] = "zip"
    return res

# ============================== ETAPA 4: ANALISAR ==============================


def carregar_oficiais():
    ofi = {}
    for r in ler_csv(PASTA / "coordenadas_oficiais.csv"):
        if r.get("lat") and r.get("lon") and r.get("h"):
            ofi[r["estacao"]] = (dms_para_graus(r["lat"]), dms_para_graus(r["lon"]), num_br(r["h"]))
    return ofi


def montar_tabela_sessoes(args):
    import pandas as pd
    ofi = carregar_oficiais()
    faltam = [s for s in args.estacoes if s not in ofi]
    if faltam:
        log(f"AVISO: sem coordenada oficial para {faltam}; preencha {PASTA / 'coordenadas_oficiais.csv'}")
    linhas = []
    for s in ler_csv(PASTA / "sessoes.csv"):
        if s["estacao"] not in args.estacoes or s["dia_alvo"] not in args.dias or s["valida"] != "sim":
            continue
        res = ler_resultado(PASTA / "ppp" / s["sessao"])
        if not res or res.get("lat") is None:
            continue
        lin = {"sessao": s["sessao"], "estacao": s["estacao"], "dia": s["dia"], "periodo": s["periodo"],
               "dur_h": int(float(s["dur_h"])), "n_epocas": int(s["n_epocas"]),
               "lat": res["lat"], "lon": res["lon"], "h": res["h"],
               "slat_mm": (res.get("slat") or float("nan")) * 1000,
               "slon_mm": (res.get("slon") or float("nan")) * 1000,
               "sh_mm": (res.get("sh") or float("nan")) * 1000,
               "orbita": res.get("orbita", ""), "referencial": res.get("referencial", ""),
               "obs_gps": res.get("obs_gps"), "obs_glonass": res.get("obs_glonass")}
        if s["estacao"] in ofi:
            dn, de, dh = diferenca_enu_mm(res["lat"], res["lon"], res["h"], *ofi[s["estacao"]])
            lin.update(dN_mm=dn, dE_mm=de, dh_mm=dh, dplan_mm=math.hypot(dn, de))
        # velocidade aplicada pelo IBGE-PPP na redução à época 2000.4 (mm/ano)
        if res.get("lat_ep") is not None and res.get("inicio"):
            dt = ano_decimal(res["inicio"]) - 2000.4
            vn, ve, _ = diferenca_enu_mm(res["lat_ep"], res["lon_ep"], res["h"], res["lat"], res["lon"], res["h"])
            lin.update(vN_aplicada_mm_ano=vn / dt, vE_aplicada_mm_ano=ve / dt,
                       dh_epoca_2000_mm=(res.get("h_ep", res["h"]) - res["h"]) * 1000)
        linhas.append(lin)
    df = pd.DataFrame(linhas)
    if df.empty:
        return df
    # diferença em relação à solução de 24 h do mesmo dia (ruído da sessão curta)
    ref = df[df.periodo == "24h"].set_index(["estacao", "dia"])[["lat", "lon", "h"]]
    for c in ("dN24_mm", "dE24_mm", "dh24_mm", "dplan24_mm"):
        df[c] = float("nan")
    for i, r in df.iterrows():
        if (r.estacao, r.dia) in ref.index and r.periodo != "24h":
            lr, lo, hr = ref.loc[(r.estacao, r.dia)]
            dn, de, dh = diferenca_enu_mm(r.lat, r.lon, r.h, lr, lo, hr)
            df.loc[i, ["dN24_mm", "dE24_mm", "dh24_mm", "dplan24_mm"]] = [dn, de, dh, math.hypot(dn, de)]
    df["splan95_mm"] = (df.slat_mm ** 2 + df.slon_mm ** 2) ** 0.5
    return df


def resumir(df, tol):
    import numpy as np
    import pandas as pd
    grupos = []
    for (per, dur), g in df.groupby(["periodo", "dur_h"]):
        linha = {"periodo": per, "dur_h": dur, "n": len(g)}
        if "dplan_mm" in g:
            p = g.dplan_mm.dropna()
            h = g.dh_mm.dropna().abs()
            linha.update(
                plan_mediana=p.median(), plan_p90=np.percentile(p, 90) if len(p) else np.nan, plan_max=p.max(),
                pct_plan_dentro_tol=100 * (p <= tol).mean() if len(p) else np.nan,
                h_mediana=h.median(), h_p90=np.percentile(h, 90) if len(h) else np.nan, h_max=h.max())
        if per != "24h":
            q = g.dplan24_mm.dropna()
            linha.update(ruido_plan_mediana=q.median(), ruido_h_mediana=g.dh24_mm.abs().median())
        linha.update(sigma_plan_relatado_mediana=g.splan95_mm.median(), sigma_h_relatado_mediana=g.sh_mm.median())
        if "dplan_mm" in g:
            linha["pct_erro_maior_que_sigma95"] = 100 * (g.dplan_mm > g.splan95_mm).mean()
        grupos.append(linha)
    t = pd.DataFrame(grupos)
    ordem = {"manha": 0, "tarde": 1, "24h": 2}
    return t.sort_values(by=["periodo", "dur_h"], key=lambda c: c.map(ordem) if c.name == "periodo" else c)


def combinacoes(df, tol):
    """Média de manhã + tarde (duas ocupações no mesmo dia) x sessão contínua equivalente."""
    import numpy as np
    import pandas as pd
    ofi = carregar_oficiais()
    out = []
    for (sta, dia), g in df[df.periodo != "24h"].groupby(["estacao", "dia"]):
        if sta not in ofi:
            continue
        durs = set(g.dur_h.unique())
        for d in sorted(x for x in durs if 2 * x in durs):  # só onde existe a sessão contínua equivalente
            m, t = g[(g.periodo == "manha") & (g.dur_h == d)], g[(g.periodo == "tarde") & (g.dur_h == d)]
            if m.empty or t.empty:
                continue
            lat = (m.lat.iloc[0] + t.lat.iloc[0]) / 2
            lon = (m.lon.iloc[0] + t.lon.iloc[0]) / 2
            h = (m.h.iloc[0] + t.h.iloc[0]) / 2
            dn, de, dh = diferenca_enu_mm(lat, lon, h, *ofi[sta])
            cm = g[(g.periodo == "manha") & (g.dur_h == 2 * d)]
            ct = g[(g.periodo == "tarde") & (g.dur_h == 2 * d)]
            out.append({"estacao": sta, "dia": dia, "combinacao": f"{d}h manha + {d}h tarde",
                        "total_h": 2 * d, "dplan_mm": math.hypot(dn, de), "dh_mm": dh,
                        "continua_manha_dplan_mm": cm.dplan_mm.iloc[0] if not cm.empty else np.nan,
                        "continua_tarde_dplan_mm": ct.dplan_mm.iloc[0] if not ct.empty else np.nan,
                        "continua_manha_dh_mm": cm.dh_mm.iloc[0] if not cm.empty else np.nan,
                        "continua_tarde_dh_mm": ct.dh_mm.iloc[0] if not ct.empty else np.nan})
    c = pd.DataFrame(out)
    if c.empty:
        return c, c
    res = c.groupby(["combinacao", "total_h"]).agg(
        n=("dplan_mm", "size"),
        plan_mediana=("dplan_mm", "median"), plan_max=("dplan_mm", "max"),
        pct_plan_dentro_tol=("dplan_mm", lambda s: 100 * (s <= tol).mean()),
        h_abs_mediana=("dh_mm", lambda s: s.abs().median()),
        continua_manha_plan_mediana=("continua_manha_dplan_mm", "median"),
        continua_tarde_plan_mediana=("continua_tarde_dplan_mm", "median"),
        continua_manha_pct_dentro_tol=("continua_manha_dplan_mm",
                                       lambda s: 100 * (s.dropna() <= tol).mean() if s.notna().any() else np.nan),
        continua_tarde_pct_dentro_tol=("continua_tarde_dplan_mm",
                                       lambda s: 100 * (s.dropna() <= tol).mean() if s.notna().any() else np.nan),
        continua_manha_h_abs_mediana=("continua_manha_dh_mm", lambda s: s.abs().median()),
        continua_tarde_h_abs_mediana=("continua_tarde_dh_mm", lambda s: s.abs().median()),
    ).reset_index().sort_values("total_h")
    return c, res


def graficos(df, tol, destino):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({"font.family": "serif",
                         "font.serif": ["Times New Roman", "Liberation Serif", "DejaVu Serif"],
                         "font.size": 9, "axes.edgecolor": "#8a8a8a", "axes.linewidth": 0.6,
                         "xtick.color": "#444444", "ytick.color": "#444444",
                         "axes.labelcolor": "#222222", "savefig.dpi": 300})
    cores = {"manha": "#2a78d6", "tarde": "#eb6834"}
    marcas = {"manha": "o", "tarde": "s"}
    nomes = {"manha": "Início às 8 h", "tarde": "Início às 13 h"}
    durs = sorted(df[df.periodo != "24h"].dur_h.unique())
    xpos = {d: i for i, d in enumerate(durs)}
    x24 = len(durs) + 0.6
    fig, eixos = plt.subplots(1, 2, figsize=(6.7, 2.9))
    for eixo, col, titulo in ((eixos[0], "dplan_mm", "(a) Planimetria"),
                              (eixos[1], "dh_abs", "(b) Altimetria (|Δh|)")):
        dd = df.copy()
        dd["dh_abs"] = dd.dh_mm.abs()
        rng = np.random.default_rng(1)
        # teto do eixo: valores muito discrepantes são desenhados no topo, com o valor escrito
        vals = dd[col].dropna()
        teto = float(np.percentile(vals, 97)) * 1.2
        if col == "dplan_mm":
            teto = max(teto, tol * 1.9)
        cortar = col == "dplan_mm" and vals.max() > teto * 1.6   # só a planimetria tem valores tão fora
        teto = math.ceil(teto / 10) * 10 if cortar else float(vals.max()) * 1.06
        for k, per in enumerate(("manha", "tarde")):
            g = dd[dd.periodo == per]
            desloc = -0.14 if per == "manha" else 0.14
            xs = g.dur_h.map(xpos) + desloc + rng.uniform(-0.05, 0.05, len(g))
            dentro = g[col] <= teto
            eixo.scatter(xs[dentro], g[col][dentro], s=9, color=cores[per], alpha=0.35, marker=marcas[per],
                         linewidths=0, zorder=2)
            for x, v in zip(xs[~dentro], g[col][~dentro]):
                eixo.scatter([x], [teto * 0.965], s=22, color=cores[per], marker="^", linewidths=0, zorder=4,
                             clip_on=False)
                eixo.text(x + 0.09, teto * 0.965, f"{v:.0f}", fontsize=7.5, va="center", ha="left",
                          color="#222222")
            med = g.groupby("dur_h")[col].median()
            eixo.plot([xpos[d] + desloc for d in med.index], med.values, color=cores[per], lw=2,
                      marker=marcas[per], ms=5, zorder=3, label=nomes[per],
                      markeredgecolor="white", markeredgewidth=0.8)
        g24 = dd[dd.periodo == "24h"]
        eixo.scatter(np.full(len(g24), x24) + rng.uniform(-0.06, 0.06, len(g24)), g24[col], s=9,
                     color="#555555", alpha=0.4, linewidths=0, zorder=2)
        eixo.plot([x24], [g24[col].median()], marker="D", ms=5, color="#333333", zorder=3,
                  markeredgecolor="white", markeredgewidth=0.8)
        if col == "dplan_mm":
            eixo.axhline(tol, color="#222222", lw=0.8, ls=(0, (4, 3)), zorder=1)
            eixo.text(x24 + 0.35, tol, f"{tol:.0f} mm", va="bottom", ha="right", fontsize=8, color="#222222")
        eixo.set_xticks(list(xpos.values()) + [x24])
        eixo.set_xticklabels([f"{d} h" for d in durs] + ["24 h"])
        eixo.set_xlim(-0.5, x24 + 0.45)
        eixo.set_ylim(0, teto)
        eixo.set_title(titulo, fontsize=9, loc="left")
        eixo.set_xlabel("Duração da sessão")
        eixo.grid(axis="y", color="#e6e6e6", lw=0.5, zorder=0)
        for lado in ("top", "right"):
            eixo.spines[lado].set_visible(False)
    eixos[0].set_ylabel("Divergência para a coordenada oficial (mm)")
    eixos[0].legend(frameon=False, fontsize=8, loc="upper right")
    fig.tight_layout(w_pad=2)
    fig.savefig(destino / "figura_divergencia_por_duracao.png", facecolor="white")
    plt.close(fig)


def etapa_analisar(args):
    import pandas as pd
    SAIDA.mkdir(parents=True, exist_ok=True)
    progresso("Analisando os resultados", 0, 1, "lendo as sessões")
    df = montar_tabela_sessoes(args)
    if df.empty:
        sys.exit("Nenhum resultado do IBGE-PPP encontrado em dados/ppp. Rode 'enviar' antes.")
    tol = TOLERANCIA_PLANI_MM
    tab = resumir(df, tol)
    comb_det, comb = combinacoes(df, tol) if "dplan_mm" in df else (pd.DataFrame(), pd.DataFrame())
    por_sta = pd.DataFrame()
    if "dplan_mm" in df:
        por_sta = df.groupby(["estacao", "periodo"]).agg(
            n=("dplan_mm", "size"), dN_medio=("dN_mm", "mean"), dE_medio=("dE_mm", "mean"),
            dh_medio=("dh_mm", "mean"), plan_max=("dplan_mm", "max"),
            pct_plan_dentro_tol=("dplan_mm", lambda s: 100 * (s <= tol).mean())).reset_index()
    df.to_csv(SAIDA / "sessoes_resultados.csv", index=False, sep=";", decimal=",", encoding="utf-8-sig")
    tab.to_csv(SAIDA / "tabela_duracao_periodo.csv", index=False, sep=";", decimal=",", encoding="utf-8-sig")
    if not comb.empty:
        comb.to_csv(SAIDA / "tabela_manha_mais_tarde.csv", index=False, sep=";", decimal=",", encoding="utf-8-sig")
    try:
        with pd.ExcelWriter(SAIDA / "resultados_ppp_sessoes.xlsx") as xw:
            tab.to_excel(xw, sheet_name="duracao_periodo", index=False)
            if not comb.empty:
                comb.to_excel(xw, sheet_name="manha_mais_tarde", index=False)
                comb_det.to_excel(xw, sheet_name="manha_mais_tarde_detalhe", index=False)
            if not por_sta.empty:
                por_sta.to_excel(xw, sheet_name="por_estacao", index=False)
            df.to_excel(xw, sheet_name="todas_as_sessoes", index=False)
    except Exception as e:  # noqa
        log(f"(xlsx não gerado: {e})")
    if "dplan_mm" in df:
        graficos(df, tol, SAIDA)
    # números para o texto do resumo
    num = {"n_sessoes": int(len(df)), "n_estacoes": int(df.estacao.nunique()),
           "dias": sorted(df.dia.unique().tolist()), "tabela": tab.round(1).to_dict("records"),
           "combinacoes": comb.round(1).to_dict("records") if not comb.empty else [],
           "por_estacao_24h": por_sta[por_sta.periodo == "24h"].round(1).to_dict("records") if not por_sta.empty else [],
           "orbitas": df.orbita.value_counts().to_dict(), "referenciais": df.referencial.value_counts().to_dict(),
           "sessoes_com_glonass": int((df.obs_glonass.fillna(0) > 0).sum()),
           "velocidade_aplicada": df.groupby("estacao")[["vN_aplicada_mm_ano", "vE_aplicada_mm_ano"]].median()
           .round(2).reset_index().to_dict("records") if "vN_aplicada_mm_ano" in df else []}
    (SAIDA / "numeros_resumo.json").write_text(json.dumps(num, ensure_ascii=False, indent=1, default=str),
                                               encoding="utf-8")
    pd.set_option("display.width", 200)
    log("\n=== Divergência para a coordenada oficial, por duração e período (mm) ===")
    cols = [c for c in ("periodo", "dur_h", "n", "plan_mediana", "plan_p90", "plan_max", "pct_plan_dentro_tol",
                        "h_mediana", "h_max", "ruido_plan_mediana") if c in tab]
    log(tab[cols].round(1).to_string(index=False))
    if not comb.empty:
        log("\n=== Duas ocupações (manhã + tarde) x sessão contínua de mesma duração total ===")
        log(comb.round(1).to_string(index=False))
    progresso("Analisando os resultados", 1, 1, "pronto")
    log(f"\nArquivos em {SAIDA}")

# ============================== PRINCIPAL ==============================


def main():
    ap = argparse.ArgumentParser(description="PPP em sessões curtas (IBGE-PPP x RBMC)")
    ap.add_argument("etapa", choices=["baixar", "cortar", "testar-api", "enviar", "analisar", "tudo"])
    ap.add_argument("--email", default=os.environ.get("PPP_EMAIL", ""))
    ap.add_argument("--dias", nargs="+", default=DIAS)
    ap.add_argument("--estacoes", nargs="+", default=ESTACOES)
    ap.add_argument("--duracoes", nargs="+", type=int, default=DURACOES_H)
    args = ap.parse_args()
    args.estacoes = [s.upper() for s in args.estacoes]
    if args.etapa in ("baixar", "tudo"):
        etapa_baixar(args)
    if args.etapa in ("cortar", "tudo"):
        etapa_cortar(args)
    if args.etapa == "testar-api":
        etapa_testar_api(args)
    if args.etapa in ("enviar", "tudo"):
        etapa_enviar(args)
    if args.etapa in ("analisar", "tudo"):
        etapa_analisar(args)


def config_padrao():
    """Configuração guardada em dados/config.json (usada pela janela)."""
    cfg = {"email": "", "dias": DIAS, "estacoes": ESTACOES, "duracoes": DURACOES_H}
    arq = PASTA / "config.json"
    if arq.exists():
        try:
            cfg.update(json.loads(arq.read_text(encoding="utf-8")))
        except ValueError:
            pass
    return cfg


def salvar_config(cfg):
    PASTA.mkdir(parents=True, exist_ok=True)
    (PASTA / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, Interrompido):
        log("Interrompido pelo usuário. Rode de novo para continuar de onde parou.")
        sys.exit(130)
