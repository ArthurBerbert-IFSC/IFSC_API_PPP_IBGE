# -*- coding: utf-8 -*-
"""
Teste de ponta a ponta com o servidor simulado (não toca no IBGE):
baixar -> cortar -> testar-api -> enviar -> analisar, para 3 estações e 1 dia.

    python tests/testar_fluxo.py
"""
import csv
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
sys.path.insert(0, str(AQUI))
import servidor_simulado as sim  # noqa: E402


def main():
    srv = sim.iniciar(0)
    porta = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = Path(tempfile.mkdtemp(prefix="ppp_teste_"))
    env = dict(os.environ, PPP_BASE=str(base),
               URL_RINEX2=f"http://127.0.0.1:{porta}/rinex2/{{ano}}/{{doy:03d}}/{{sta}}{{doy:03d}}1.zip",
               URL_RINEX3=f"http://127.0.0.1:{porta}/rinex3/{{sta}}/{{ano}}/{{doy}}",
               URLS_RELATORIO=f"http://127.0.0.1:{porta}/relatorio/{{STA}}.pdf",
               URL_PPP=f"http://127.0.0.1:{porta}/ppp", PYTHONIOENCODING="utf-8")
    estacoes = ["POAL", "RNNA", "SALU"]
    comum = ["--estacoes", *estacoes, "--dias", "2026-08-12"]
    prog = [sys.executable, str(RAIZ / "ppp_sessoes.py")]

    def rodar(*etapa):
        r = subprocess.run(prog + list(etapa) + comum, env=env, capture_output=True, text=True, encoding="utf-8")
        print(f"--- {etapa[0]}: código {r.returncode}")
        if r.returncode != 0:
            print(r.stdout[-3000:], r.stderr[-3000:])
            raise SystemExit(f"falhou em {etapa[0]}")
        return r.stdout

    rodar("baixar")
    # sem relatórios no servidor simulado: coordenadas oficiais escritas aqui
    with open(base / "dados" / "coordenadas_oficiais.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["estacao", "sat", "lat", "lon", "h", "fonte"])
        for s in estacoes:
            w.writerow([s, "", *sim.EST[s], "simulada"])
    rodar("cortar")
    sessoes = list(csv.DictReader(open(base / "dados" / "sessoes.csv", encoding="utf-8")))
    assert len(sessoes) == 27, len(sessoes)
    assert all(s["cobertura"] == "1.000" for s in sessoes), "épocas faltando no corte"
    saida = rodar("testar-api", "--email", "teste@exemplo.invalid")
    assert "TESTE OK" in saida
    variante = json.loads((base / "dados" / "api_variante.json").read_text(encoding="utf-8"))
    assert variante["antena"] == "igs", variante
    rodar("enviar", "--email", "teste@exemplo.invalid")
    assert len(list((base / "dados" / "ppp").glob("*/resumo.json"))) == 27
    rodar("analisar")
    num = json.loads((base / "saida" / "numeros_resumo.json").read_text(encoding="utf-8"))
    for r in num["por_estacao_24h"]:
        vn, ve, vh = sim.VIES[r["estacao"]]
        assert abs(r["dN_medio"] - vn) < 15 and abs(r["dE_medio"] - ve) < 15, (r, sim.VIES[r["estacao"]])
    assert (base / "saida" / "figura_divergencia_por_duracao.png").exists()
    print(f"\nTUDO CERTO. Arquivos do teste em {base}")


if __name__ == "__main__":
    main()
