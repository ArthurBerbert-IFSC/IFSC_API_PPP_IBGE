# -*- coding: utf-8 -*-
"""
Servidor local que imita, com dados SINTÉTICOS, o geoftp/servicodados do IBGE e a API do IBGE-PPP.
Serve só para testar o fluxo do programa sem tocar no servidor real.

- GET  /rinex2/<ano>/<doy>/<sta><doy>1.zip   RINEX 2.11 diário (Hatanaka, dentro de ZIP)
- GET  /rinex3/<sta>/<ano>/<doy>             RINEX 3.04 diário (.crx.gz)   [SALU só tem RINEX 3]
- POST /ppp                                  resposta no formato da API real (status, link, resumo)

Regras simuladas: a API recusa (firewall) um User-Agent "bloqueado" e só reconhece a antena
no formato IGS (modelo com 16 colunas + radome).
"""
import io
import json
import math
import random
import re
import sys
import zipfile
from datetime import datetime, timedelta
from email.parser import BytesParser
from email.policy import default as politica
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import hatanaka

EST = {  # lat, lon, h (sintéticos, próximos das estações reais)
    "BOAV": (2.8452, -60.7011, 69.0), "BRAZ": (-15.9475, -47.8779, 1106.0),
    "CEFE": (-20.3108, -40.3195, 14.0), "MTSR": (-12.5452, -55.7275, 390.0),
    "POAL": (-30.0740, -51.1198, 76.7), "POLI": (-23.5556, -46.7303, 730.0),
    "RNNA": (-5.8361, -35.2077, 45.9), "ROSA": (-22.5237, -52.9521, 318.0),
    "SALU": (-2.5935, -44.2125, 18.9),
}
_r = random.Random(7)
VIES = {s: (_r.uniform(-10, 30), _r.uniform(-50, 0), _r.uniform(-30, 60)) for s in EST}  # mm
A, F = 6378137.0, 1 / 298.257222101
E2 = F * (2 - F)


def geo2xyz(lat, lon, h):
    la, lo = math.radians(lat), math.radians(lon)
    n = A / math.sqrt(1 - E2 * math.sin(la) ** 2)
    return ((n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo),
            (n * (1 - E2) + h) * math.sin(la))


def mm2graus(lat, dn, de):
    phi = math.radians(lat)
    m = A * (1 - E2) / (1 - E2 * math.sin(phi) ** 2) ** 1.5
    n = A / math.sqrt(1 - E2 * math.sin(phi) ** 2)
    return math.degrees(dn / 1000 / m), math.degrees(de / 1000 / (n * math.cos(phi)))


def gms_br(g):
    s = "-" if g < 0 else ""
    g = abs(g)
    d = int(g)
    m = int((g - d) * 60)
    sec = (g - d - m / 60) * 3600
    return f"{s}{d} {m:02d} {sec:07.4f}".replace(".", ",")


TIPOS2 = ["C1", "L1", "L2", "P2", "C2", "S1", "S2", "D1", "D2"]


def rinex2(sta, dia):
    x, y, z = geo2xyz(*EST[sta])
    cab = [
        f"{'     2.11':<20s}{'OBSERVATION DATA':<20s}{'M (MIXED)':<20s}RINEX VERSION / TYPE",
        f"{sta:<60s}MARKER NAME",
        f"{'5678':<20s}{'TRM115000.00':<16s}{'NONE':<4s}{'':<20s}ANT # / TYPE",
        f"{x:14.4f}{y:14.4f}{z:14.4f}{'':<18s}APPROX POSITION XYZ",
        f"{0.0080:14.4f}{0.0:14.4f}{0.0:14.4f}{'':<18s}ANTENNA: DELTA H/E/N",
        f"{len(TIPOS2):6d}" + "".join(f"{t:>6s}" for t in TIPOS2) + f"{'':<{54 - 6 * len(TIPOS2)}s}# / TYPES OF OBSERV",
        f"{15.0:10.3f}{'':<50s}INTERVAL",
        f"{dia.year:6d}{dia.month:6d}{dia.day:6d}{0:6d}{0:6d}{0.0:13.7f}     GPS         TIME OF FIRST OBS",
        f"{'':<60s}END OF HEADER",
    ]
    corpo = []
    sats = [f"G{i:02d}" for i in range(1, 17)] + [f"R{i:02d}" for i in range(1, 9)]
    t = datetime(dia.year, dia.month, dia.day)
    for k in range(5760):
        tt = t + timedelta(seconds=15 * k)
        vis = sats[(k // 240) % 5: (k // 240) % 5 + 18]  # 18 satélites: testa a linha de continuação
        lista = "".join(vis)
        corpo.append(f" {tt.year % 100:02d} {tt.month:2d} {tt.day:2d} {tt.hour:2d} {tt.minute:2d}"
                     f"{tt.second:11.7f}  0{len(vis):3d}{lista[:36]}")
        resto = lista[36:]
        while resto:
            corpo.append(f"{'':32s}{resto[:36]}")
            resto = resto[36:]
        for _ in vis:
            obs = [f"{20000000 + k * 3.1 + i:14.3f}  " for i in range(9)]
            corpo.append("".join(obs[:5]).rstrip())
            corpo.append("".join(obs[5:]).rstrip())
        if k == 2000:  # evento com registro de cabeçalho no meio do arquivo
            corpo.append(f"{'':26s}  4  1")
            corpo.append(f"{'evento sintetico de teste':<60s}COMMENT")
    return "\n".join(cab + corpo) + "\n"


def rinex3(sta, dia):
    x, y, z = geo2xyz(*EST[sta])
    tipos = ["C1C", "L1C", "S1C", "C2W", "L2W", "S2W"]
    cab = [
        f"{'     3.04':<20s}{'OBSERVATION DATA':<20s}{'M':<20s}RINEX VERSION / TYPE",
        f"{sta:<60s}MARKER NAME",
        f"{'5678':<20s}{'TRM115000.00':<16s}{'NONE':<4s}{'':<20s}ANT # / TYPE",
        f"{x:14.4f}{y:14.4f}{z:14.4f}{'':<18s}APPROX POSITION XYZ",
        f"{0.0080:14.4f}{0.0:14.4f}{0.0:14.4f}{'':<18s}ANTENNA: DELTA H/E/N",
        f"G{len(tipos):5d} " + " ".join(tipos) + f"{'':<{54 - 4 * len(tipos)}s}SYS / # / OBS TYPES",
        f"R{len(tipos):5d} " + " ".join(tipos) + f"{'':<{54 - 4 * len(tipos)}s}SYS / # / OBS TYPES",
        f"{15.0:10.3f}{'':<50s}INTERVAL",
        f"{dia.year:6d}{dia.month:6d}{dia.day:6d}{0:6d}{0:6d}{0.0:13.7f}     GPS         TIME OF FIRST OBS",
        f"{'':<60s}END OF HEADER",
    ]
    corpo = []
    sats = [f"G{i:02d}" for i in range(1, 13)] + [f"R{i:02d}" for i in range(1, 7)]
    t = datetime(dia.year, dia.month, dia.day)
    for k in range(5760):
        tt = t + timedelta(seconds=15 * k)
        corpo.append(f"> {tt.year:4d} {tt.month:02d} {tt.day:02d} {tt.hour:02d} {tt.minute:02d}"
                     f"{tt.second:11.7f}  0{len(sats):3d}")
        for sv in sats:
            corpo.append(sv + "".join(f"{20000000 + k * 2.7 + i:14.3f}  " for i in range(6)).rstrip())
    return "\n".join(cab + corpo) + "\n"


_cache = {}


def zip_rinex2(sta, ano, doy):
    if (sta, ano, doy) not in _cache:
        dia = datetime(ano, 1, 1) + timedelta(days=doy - 1)
        crx = hatanaka.compress(rinex2(sta, dia).encode("latin-1"), compression="none")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{sta.lower()}{doy:03d}1.{ano % 100:02d}d", crx)
            z.writestr(f"{sta.lower()}{doy:03d}1.{ano % 100:02d}n", "navegacao sintetica\n")
        _cache[(sta, ano, doy)] = buf.getvalue()
    return _cache[(sta, ano, doy)]


def resumo_ppp(nome, rinex_txt, antena):
    """Monta o 'resumo' como a API real devolve (vírgula decimal, graus-minutos-segundos)."""
    linhas = rinex_txt.splitlines()
    sta = next(l[:4] for l in linhas if l[60:].strip() == "MARKER NAME")
    fim = next(i for i, l in enumerate(linhas) if l[60:73] == "END OF HEADER")
    v3 = linhas[0][:9].strip().startswith("3")
    tempos = []
    for l in linhas[fim + 1:]:
        if v3 and l.startswith(">"):
            tempos.append(datetime(int(l[2:6]), int(l[7:9]), int(l[10:12]), int(l[13:15]), int(l[16:18])))
        elif not v3 and re.match(r"^ \d\d [ \d]\d [ \d]\d [ \d]\d [ \d]\d", l) and l[28] == "0":
            tempos.append(datetime(2000 + int(l[1:3]), int(l[4:6]), int(l[7:9]), int(l[10:12]), int(l[13:15])))
    dur = (tempos[-1] - tempos[0]).total_seconds() / 3600 + 15 / 3600
    hsl = (tempos[0].hour + tempos[0].minute / 60 + EST[sta][1] / 15) % 24
    tarde = 11 < hsl < 20 and dur < 20
    r = random.Random(nome)
    sp, sh = (3.0, 6.0) if dur > 20 else (35 / math.sqrt(dur) * (1.6 if tarde else 1.0),
                                           55 / math.sqrt(dur) * (1.5 if tarde else 1.0))
    bn, be, bh = VIES[sta]
    lat, lon, h = EST[sta]
    dla, dlo = mm2graus(lat, bn + r.gauss(0, sp), be + r.gauss(0, sp))
    reconhecida = antena is not None and "    " in antena
    return {"PROCES": f"{datetime.now():%Y/%m/%d %H:%M:%S}", "RINEX": nome, "MARCO": sta,
            "INICIO": f"{tempos[0]:%Y/%m/%d %H:%M:%S},00", "MODO": "ESTATICO",
            "ANTENA": antena.strip() if reconhecida else "NÃO DISPONÍVEL", "ALTANT": "0,008 m",
            "FREQ": "L3", "OBSERV": "CODIGO&FASE", "ORBITA": "FINAL", "DATUM": "SIRGAS2000, época 2000.4",
            "LAT": gms_br(lat + dla), "LON": gms_br(lon + dlo),
            "HGEO": f"{h + (bh + r.gauss(0, sh)) / 1000:.2f} m".replace(".", ","),
            "SLAT": f"{sp / 2200:.3f} m".replace(".", ","), "SLON": f"{sp / 1600:.3f} m".replace(".", ","),
            "SHGEO": f"{sh / 2000:.3f} m".replace(".", ","), "": ""}


class Tratador(BaseHTTPRequestHandler):
    ua_bloqueado = "bloqueado"

    def log_message(self, *a):
        pass

    def responder(self, st, corpo, tipo="application/octet-stream"):
        self.send_response(st)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        m = re.match(r"/rinex2/(\d{4})/(\d{3})/([a-z]{4})\d{3}1\.zip", self.path)
        if m:
            sta = m.group(3).upper()
            if sta not in EST or sta == "SALU":
                return self.responder(404, b"")
            return self.responder(200, zip_rinex2(sta, int(m.group(1)), int(m.group(2))), "application/zip")
        m = re.match(r"/rinex3/([a-z]{4})/(\d{4})/(\d+)", self.path)
        if m:
            sta = m.group(1).upper()
            if sta not in EST:
                return self.responder(404, b"")
            dia = datetime(int(m.group(2)), 1, 1) + timedelta(days=int(m.group(3)) - 1)
            return self.responder(200, hatanaka.compress(rinex3(sta, dia).encode(), compression="gz"))
        self.responder(404, b"")

    def do_POST(self):
        if self.path != "/ppp":
            return self.responder(404, b"")
        if self.headers.get("User-Agent", "").startswith(self.ua_bloqueado):
            return self.responder(200, b"<html><head><title>Request Rejected</title></head><body>The requested "
                                       b"URL was rejected.<br><br>Your support ID is: 123</body></html>",
                                  "text/html; charset=utf-8")
        corpo = self.rfile.read(int(self.headers["Content-Length"]))
        msg = BytesParser(policy=politica).parsebytes(
            b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + corpo)
        campos, arquivo = {}, None
        for parte in msg.iter_parts():
            nome = parte.get_param("name", header="content-disposition")
            dados = parte.get_payload(decode=True)
            if parte.get_filename():
                arquivo = (parte.get_filename(), dados)
            else:
                campos[nome] = dados.decode()
        faltam = [c for c in ("tipo-levantamento", "altura-antena", "email") if c not in campos]
        if arquivo is None or faltam:
            return self.responder(400, json.dumps({"erro": f"faltam {faltam}"}).encode(), "application/json")
        nome, bruto = arquivo
        if bruto[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(bruto)) as z:
                nome = z.namelist()[0]
                bruto = z.read(nome)
        rin = hatanaka.decompress(bruto).decode("latin-1")
        resumo = resumo_ppp(nome, rin, campos.get("modelo-antena"))
        resp = {"status": "OK", "link": "https://exemplo.invalid/ppp/processados_ppp/x.zip", "resumo": resumo}
        self.responder(200, json.dumps(resp, ensure_ascii=False).encode(), "application/json")


def iniciar(porta=0):
    srv = ThreadingHTTPServer(("127.0.0.1", porta), Tratador)
    return srv


if __name__ == "__main__":
    s = iniciar(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print(f"servidor simulado em http://127.0.0.1:{s.server_address[1]}", flush=True)
    s.serve_forever()
