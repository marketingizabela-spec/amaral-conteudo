#!/usr/bin/env python3
"""Edição em lote: tira o início e a fala do entrevistador, mantém só o apresentador
e coloca a caixinha de pergunta correspondente.

Dois passos:
  1) analisar    -> gera plano.csv (início do corte, trechos a silenciar, caixinha sugerida)
  2) renderizar  -> lê plano.csv (já revisado por você) e gera os vídeos prontos
"""
import argparse
import csv
import json
import re
import subprocess
import sys
import unicodedata
import wave
from pathlib import Path

VIDEO_EXT = {".mov", ".mp4", ".m4v"}
COLUNAS = ["arquivo", "inicio", "silenciar", "caixinha", "pergunta_detectada", "confianca", "revisar"]
PAD_CORTE = 0.8       # segundos antes da 1ª fala do apresentador (padrão de --folga-inicio)
PAD_SILENCIO = 0.15   # folga em volta de cada trecho silenciado
SR = 16000


def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", " ", s)


def parse_t(s):
    """Aceita segundos (12.5) ou mm:ss(.d) / h:mm:ss."""
    s = str(s).strip().replace(",", ".")
    if not s:
        return 0.0
    total = 0.0
    for p in s.split(":"):
        total = total * 60 + float(p)
    return total


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.exit(f"Erro rodando {cmd[0]}:\n{r.stderr[-2000:]}")
    return r.stdout


def probe_dims(video):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height:stream_side_data=rotation",
               "-of", "json", str(video)])
    st = json.loads(out)["streams"][0]
    w, h = int(st["width"]), int(st["height"])
    rot = 0
    for sd in st.get("side_data_list", []):
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if abs(rot) % 180 == 90:  # ffmpeg gira o quadro automaticamente
        w, h = h, w
    return w, h


def probe_dur(video):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(video)])
    return float(out.strip())


def topo_do_cabelo(video, inicio, fim):
    """Estima, em fração da altura do quadro, onde começa o cabelo (topete) do apresentador.
    Devolve None se não achar rosto ou se o OpenCV não estiver instalado."""
    try:
        import cv2
        import tempfile
    except ImportError:
        return None
    detectores = [cv2.CascadeClassifier(cv2.data.haarcascades + n)
                  for n in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml")]
    topos = []
    with tempfile.TemporaryDirectory() as tmp:
        for k in range(1, 10):
            t = inicio + (fim - inicio) * k / 10
            img = str(Path(tmp) / f"f{k}.png")
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
                 "-frames:v", "1", "-vf", "scale=480:-2", img])
            quadro = cv2.imread(img)
            if quadro is None:
                continue
            cinza = cv2.cvtColor(quadro, cv2.COLOR_BGR2GRAY)
            rostos = []
            for clf in detectores:
                rostos = clf.detectMultiScale(cinza, 1.1, 4, minSize=(50, 50))
                if len(rostos):
                    break
            if len(rostos):
                x, y, w, h = max(rostos, key=lambda r: r[2] * r[3])
                topos.append((y - 0.30 * h) / quadro.shape[0])  # cabelo/topete acima da testa
    return min(topos) if topos else None


# ---------------------------------------------------------------- analisar
def carregar_wav(path):
    import numpy as np
    with wave.open(str(path)) as f:
        data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
    return data.astype("float32") / 32768.0


def extrair_wav(video, destino):
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1",
         "-ar", str(SR), str(destino)])


def embed(encoder, wav, ini, fim):
    from resemblyzer import preprocess_wav
    trecho = wav[int(ini * SR):int(fim * SR)]
    if len(trecho) < 0.5 * SR:
        return None
    try:
        return encoder.embed_utterance(preprocess_wav(trecho, source_sr=SR))
    except Exception:
        return None


def classificar(segs, embs, seed, limiar):
    """True = é a minha voz. Semente = amostra de voz ou o 1º trecho do vídeo."""
    import numpy as np
    if seed is None:
        seed = next((e for e in embs if e is not None), None)
    if seed is None:
        return [False] * len(segs)
    for _ in range(2):  # refina o centro com quem já foi marcado como "eu"
        meus = [e for e in embs if e is not None and float(np.dot(e, seed)) >= limiar]
        if meus:
            seed = np.mean(meus + [seed], axis=0)
            seed = seed / np.linalg.norm(seed)
    marcas, anterior = [], True
    for e in embs:
        marcas.append(anterior if e is None else float(np.dot(e, seed)) >= limiar)
        anterior = marcas[-1]
    return marcas


def melhor_caixinha(texto, caixinhas):
    from rapidfuzz import fuzz
    palavras = norm(texto).split()
    if not palavras:
        return None, 0.0, 0.0
    pontos = []
    for num, frase in caixinhas.items():
        chave = [w for w in norm(frase).split() if len(w) > 3]
        if not chave:
            continue
        sc = sum(max(fuzz.ratio(w, p) for p in palavras) for w in chave) / len(chave)
        pontos.append((sc, int(num)))
    pontos.sort(reverse=True)
    segundo = pontos[1][0] if len(pontos) > 1 else 0.0
    return pontos[0][1], pontos[0][0], pontos[0][0] - segundo


def cmd_analisar(a):
    from faster_whisper import WhisperModel
    from resemblyzer import VoiceEncoder
    caixinhas = json.load(open(a.caixinhas_json, encoding="utf-8"))
    videos = sorted(p for p in Path(a.videos).iterdir() if p.suffix.lower() in VIDEO_EXT)
    if not videos:
        sys.exit(f"Nenhum vídeo em {a.videos}")
    whisper = WhisperModel(a.modelo, device="auto", compute_type="int8")
    encoder = VoiceEncoder()
    seed = None
    if a.voz_minha:
        from resemblyzer import preprocess_wav
        seed = encoder.embed_utterance(preprocess_wav(a.voz_minha))
    tmp = Path(a.plano).with_suffix(".tmp.wav")
    linhas, usadas = [], {}
    for v in videos:
        print(f"Analisando {v.name} ...", flush=True)
        extrair_wav(v, tmp)
        wav = carregar_wav(tmp)
        segs = [(s.start, s.end, s.text.strip())
                for s in whisper.transcribe(str(tmp), language="pt", vad_filter=True)[0]]
        if not segs:
            linhas.append({"arquivo": v.name, "revisar": "SEM FALA DETECTADA"})
            continue
        embs = [embed(encoder, wav, s, e) for s, e, _ in segs]
        meu = classificar(segs, embs, seed, a.limiar)
        apres = [i for i, m in enumerate(meu) if not m]
        if not apres:
            linhas.append({"arquivo": v.name, "revisar": "NÃO ACHEI O APRESENTADOR"})
            continue
        primeiro = apres[0]
        inicio = max(0.0, segs[primeiro][0] - a.folga_inicio)
        da_apres = [(s, e) for (s, e, _), m in zip(segs, meu) if not m]

        def folgar(s, e):
            """Folga em volta da minha fala, sem invadir a fala do apresentador."""
            s2, e2 = s - PAD_SILENCIO, e + PAD_SILENCIO
            for ps, pe in da_apres:
                if ps >= e:
                    e2 = min(e2, max(e, ps - 0.05))
                if pe <= s:
                    s2 = max(s2, min(s, pe + 0.05))
            return max(inicio, s2), e2

        silenciar = [folgar(s, e) for (s, e, _), m in zip(segs, meu) if m and e > inicio]
        pergunta = " ".join(t for (s, e, t), m in zip(segs, meu) if m and s < inicio)
        base = pergunta or segs[primeiro][2]
        num, sc, folga = melhor_caixinha(base, caixinhas)
        aviso = []
        if sc < 70 or folga < 8:
            aviso.append("CONFERIR CAIXINHA")
        if num in usadas:
            aviso.append(f"caixinha repetida (também em {usadas[num]})")
        usadas.setdefault(num, v.name)
        linhas.append({
            "arquivo": v.name, "inicio": f"{inicio:.2f}",
            "silenciar": "|".join(f"{s:.2f}-{e:.2f}" for s, e in silenciar),
            "caixinha": num, "pergunta_detectada": base[:160],
            "confianca": f"{sc:.0f}", "revisar": "; ".join(aviso)})
    tmp.unlink(missing_ok=True)
    with open(a.plano, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, COLUNAS, delimiter=";")
        w.writeheader()
        w.writerows(linhas)
    print(f"\nPlano salvo em {a.plano}. Abra no Excel, confira a coluna 'revisar' e rode 'renderizar'.")


# -------------------------------------------------------------- renderizar
# HLG (HDR do iPhone) -> SDR BT.709, só para o modo --sdr e para as imagens de prévia
TONEMAP = ("zscale=transfer=linear:npl=1000,format=gbrpf32le,zscale=primaries=bt709,"
           "tonemap=tonemap=mobius:desat=0,zscale=transfer=bt709:matrix=bt709:range=tv,format=yuv420p")


def probe_cor(video):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=color_transfer,pix_fmt", "-of", "json", str(video)])
    st = json.loads(out)["streams"][0]
    return st.get("color_transfer", ""), st.get("pix_fmt", "")


def escolher_modo(video, a, nome):
    """'normal' (SDR), 'hlg' (mantém o HDR original) ou 'sdr' (converte HDR para SDR)."""
    trc, _ = probe_cor(video)
    if a.sdr:
        return "sdr" if trc in ("arib-std-b67", "smpte2084") else "normal"
    if trc == "arib-std-b67":
        return "hlg"
    if trc == "smpte2084":
        print(f"  AVISO {nome}: HDR PQ; convertendo para SDR.")
        return "sdr"
    return "normal"


def montar_filtro(w, reduzir, silenciar_rel, a, frac, margem, so_video=False, modo="normal"):
    """w = largura do vídeo de saída; reduzir=True escala o vídeo para essa largura."""
    larg = int(w * frac) // 2 * 2
    if a.posicao == "topo":
        y = f"main_h*{margem}"
    elif a.posicao == "baixo":
        y = f"main_h-overlay_h-main_h*{margem}"
    else:
        y = "(main_h-overlay_h)/2"
    enable = f":enable='between(t,0,{a.caixinha_segundos})'" if a.caixinha_segundos > 0 else ""
    if silenciar_rel:
        expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in silenciar_rel)
        audio = f"[0:a]volume=0:enable='{expr}'[a]"
    else:
        audio = "[0:a]anull[a]"
    etapas = []
    entrada = "[0:v]"
    if reduzir:
        etapas.append(f"{entrada}scale={w}:-2[base]")
        entrada = "[base]"
    if modo == "sdr":
        etapas.append(f"{entrada}{TONEMAP}[sdr]")
        entrada = "[sdr]"
    if modo == "hlg":  # caixinha (sRGB) convertida para HLG BT.2020, 10 bits, com transparência
        etapas.append(
            f"[1:v]scale={larg}:-2,format=rgba,split[c1][c2];[c2]alphaextract[al];"
            "[c1]format=gbrp,zscale=tin=iec61966-2-1:t=linear,format=gbrpf32le,"
            "zscale=pin=bt709:p=bt2020:t=arib-std-b67:m=bt2020nc:r=tv,format=yuv420p10le,"
            f"lutyuv=y='clip(64+(val-64)*{a.brilho_caixinha},64,1023)'[cy];[cy][al]alphamerge[c]")
        fmt = ":format=yuv420p10"
    else:  # SDR: matriz BT.709 explícita (o padrão do ffmpeg seria BT.601)
        etapas.append(f"[1:v]scale={larg}:-2:out_color_matrix=bt709,format=yuva420p[c]")
        fmt = ":format=yuv420"
    etapas.append(f"{entrada}[c]overlay=x=(main_w-overlay_w)/2:y={y}:shortest=1{fmt}{enable}[v]")
    video = ";".join(etapas)
    return video if so_video else f"{audio};{video}"


def para_linha(a, r):
    """Se o plano.csv tiver a coluna 'largura' preenchida, ela vale só para aquele vídeo (ex.: 0.40)."""
    import copy
    valor = (r.get("largura") or "").strip().replace(",", ".")
    if not valor:
        return a
    b = copy.copy(a)
    b.largura = float(valor)
    return b


def calcular_layout(video, png, inicio, a, nome):
    """Largura de saída, redução, largura da caixinha (fração) e margem do topo (fração)."""
    w, h = probe_dims(video)
    reduzir = bool(a.largura_saida) and w > a.largura_saida
    if reduzir:
        h = int(h * a.largura_saida / w)
        w = a.largura_saida // 2 * 2
    margem = a.margem if a.margem is not None else {"topo": 0.14, "baixo": 0.35}.get(a.posicao, 0)
    frac = a.largura
    if frac is None:  # ajuste automático: a caixinha termina acima do topete
        frac = 0.40  # sem rosto detectado: tamanho pequeno para não arriscar o topete
        topo_cab = topo_do_cabelo(video, inicio, probe_dur(video)) if a.posicao == "topo" else None
        if topo_cab is None:
            print(f"  AVISO {nome}: rosto não detectado, usando {frac:.0%} da largura. Confira o resultado.")
        else:
            pw, ph = probe_dims(png)
            livre = topo_cab - a.folga_cabeca - margem  # fração da altura entre a margem e o cabelo
            ideal = livre * h * (pw / ph) / w
            frac = min(max(ideal, 0.30), a.largura_max)
            print(f"  topete a {topo_cab:.0%} da altura; caixinha com {frac:.0%} da largura, "
                  f"a {margem:.0%} do topo")
            if ideal < 0.30:
                print(f"  AVISO {nome}: pouco espaço acima da cabeça; a caixinha pode tocar o cabelo.")
    return w, reduzir, frac, margem


def codec_video(modo):
    if modo == "hlg":  # mantém as cores originais: HEVC 10 bits, BT.2020 HLG
        return ["-c:v", "libx265", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p10le",
                "-tag:v", "hvc1", "-colorspace", "bt2020nc", "-color_primaries", "bt2020",
                "-color_trc", "arib-std-b67", "-color_range", "tv",
                "-x265-params", "colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc:range=limited"]
    cores = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"] if modo == "sdr" else []
    return ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p", *cores]


def cmd_renderizar(a):
    saida = Path(a.saida)
    saida.mkdir(parents=True, exist_ok=True)
    with open(a.plano, newline="", encoding="utf-8-sig") as f:
        linhas = list(csv.DictReader(f, delimiter=";"))
    for r in linhas:
        nome = r["arquivo"]
        if not r.get("caixinha") or not r.get("inicio"):
            print(f"PULANDO {nome}: sem início/caixinha no plano ({r.get('revisar','')})")
            continue
        video = Path(a.videos) / nome
        png = Path(a.caixinhas) / f"{int(r['caixinha']):02d}.png"
        for p in (video, png):
            if not p.exists():
                sys.exit(f"Arquivo não encontrado: {p}")
        inicio = parse_t(r["inicio"])
        sil = []
        for trecho in filter(None, (r.get("silenciar") or "").split("|")):
            s, e = (parse_t(x) for x in trecho.split("-", 1))
            if e > inicio:
                sil.append((max(0.0, s - inicio), e - inicio))
        w, reduzir, frac, margem = calcular_layout(video, png, inicio, para_linha(a, r), nome)
        script = saida / f".{video.stem}.filtro.txt"
        modo = escolher_modo(video, a, nome)
        script.write_text(montar_filtro(w, reduzir, sil, a, frac, margem, modo=modo), encoding="utf-8")
        destino = saida / f"{video.stem}_editado.mp4"
        print(f"Renderizando {nome} -> {destino.name} (caixinha {int(r['caixinha']):02d})", flush=True)
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{inicio:.3f}", "-i", str(video),
             "-loop", "1", "-framerate", "30", "-i", str(png),
             "-filter_complex_script", str(script), "-map", "[v]", "-map", "[a]",
             *codec_video(modo),
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(destino)])
        script.unlink(missing_ok=True)
    print("Pronto.")


def cmd_previa(a):
    """Uma imagem por vídeo, com a caixinha no lugar, para aprovar a altura antes de renderizar."""
    saida = Path(a.saida)
    saida.mkdir(parents=True, exist_ok=True)
    with open(a.plano, newline="", encoding="utf-8-sig") as f:
        linhas = list(csv.DictReader(f, delimiter=";"))
    for r in linhas:
        nome = r["arquivo"]
        if not r.get("caixinha") or not r.get("inicio"):
            print(f"PULANDO {nome}: sem início/caixinha no plano")
            continue
        video = Path(a.videos) / nome
        png = Path(a.caixinhas) / f"{int(r['caixinha']):02d}.png"
        for p in (video, png):
            if not p.exists():
                sys.exit(f"Arquivo não encontrado: {p}")
        inicio = parse_t(r["inicio"])
        w, reduzir, frac, margem = calcular_layout(video, png, inicio, para_linha(a, r), nome)
        modo = escolher_modo(video, a, nome)
        filtro = montar_filtro(w, reduzir, [], a, frac, margem, so_video=True, modo=modo)
        if modo == "hlg":  # a imagem da prévia é SDR; o vídeo final mantém o HDR
            filtro = filtro.replace("[v]", f"[hv];[hv]{TONEMAP}[v]")
        if not a.sem_guia:  # linha vermelha = limite da zona segura de anúncio (só na prévia)
            filtro = filtro.replace("[v]", "[vv];[vv]drawbox=x=0:y=ih*%s:w=iw:h=3:color=red@0.9:t=fill[v]" % margem)
        t = min(inicio + 1.0, max(inicio, probe_dur(video) - 0.2))
        destino = saida / f"{Path(nome).stem}_previa.png"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
             "-loop", "1", "-framerate", "30", "-i", str(png),
             "-filter_complex", filtro + ";[v]scale=720:-2[p]", "-map", "[p]",
             "-frames:v", "1", str(destino)])
        print(f"Prévia: {destino}")


# ---------------------------------------------------------------- planilha
def ler_legendas(path):
    txt = Path(path).read_text(encoding="utf-8")
    achados = re.findall(r"\*\*(\d+) · (.+?)\*\*\n(.+)", txt)
    return {int(n): (titulo.strip(), legenda.strip()) for n, titulo, legenda in achados}


def cmd_planilha(a):
    import datetime as dt
    legendas = ler_legendas(a.legendas)
    with open(a.plano, newline="", encoding="utf-8-sig") as f:
        linhas = [r for r in csv.DictReader(f, delimiter=";") if r.get("caixinha")]
    horas = [h.strip() for h in a.horas.split(",") if h.strip()]
    dia0 = dt.date.fromisoformat(a.inicio) if a.inicio else dt.date.today() + dt.timedelta(days=1)
    with open(a.saida, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["arquivo_editado", "caixinha", "pergunta", "legenda", "data", "hora"])
        for i, r in enumerate(linhas):
            n = int(r["caixinha"])
            titulo, legenda = legendas.get(n, ("", "(sem legenda para esta caixinha)"))
            dia = dia0 + dt.timedelta(days=i // len(horas))
            w.writerow([Path(r["arquivo"]).stem + "_editado.mp4", f"{n:02d}", titulo, legenda,
                        dia.strftime("%d/%m/%Y"), horas[i % len(horas)]])
    print(f"{len(linhas)} postagens em {a.saida}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    an = sub.add_parser("analisar", help="transcreve, separa as vozes e sugere o plano")
    an.add_argument("--videos", required=True)
    an.add_argument("--plano", default="plano.csv")
    an.add_argument("--caixinhas-json", default=str(Path(__file__).with_name("caixinhas.json")))
    an.add_argument("--modelo", default="small", help="modelo Whisper: small, medium, large-v3")
    an.add_argument("--voz-minha", help="áudio curto (.wav) só com a sua voz; melhora a separação")
    an.add_argument("--folga-inicio", type=float, default=PAD_CORTE,
                    help="segundos mantidos antes da 1ª fala do apresentador (padrão 0.8; aumente se cortar a fala)")
    an.add_argument("--limiar", type=float, default=0.75, help="similaridade de voz (0-1) para ser 'eu'")
    an.set_defaults(fn=cmd_analisar)
    rd = sub.add_parser("renderizar", help="gera os vídeos a partir do plano.csv")
    rd.add_argument("--videos", required=True)
    rd.add_argument("--caixinhas", required=True, help="pasta com 01.png ... 50.png")
    rd.add_argument("--plano", default="plano.csv")
    rd.add_argument("--saida", default="saida")
    rd.add_argument("--posicao", choices=["topo", "centro", "baixo"], default="topo")
    rd.add_argument("--largura", type=float, default=None,
                    help="largura fixa da caixinha (fração do vídeo); sem isso ela se ajusta sozinha para não cobrir a cabeça")
    rd.add_argument("--sdr", action="store_true",
                    help="converte vídeos HDR do iPhone para SDR (H.264, compatível com tudo). "
                         "Padrão: mantém as cores originais (HEVC HDR HLG)")
    rd.add_argument("--brilho-caixinha", type=float, default=1.35,
                    help="só HDR: brilho do branco da caixinha (1.0 = padrão de gráficos, ~75%% do sinal HLG; "
                         "1.35 iguala às áreas claras do vídeo). Suba se a caixinha parecer cinza, desça se ofuscar")
    rd.add_argument("--largura-max", type=float, default=0.7, help="largura máxima no ajuste automático")
    rd.add_argument("--margem", type=float, default=None,
                    help="distância da borda (fração da altura). Padrão: 0.14 no topo (zona segura de anúncio da Meta)")
    rd.add_argument("--folga-cabeca", type=float, default=0.03,
                    help="espaço livre entre a caixinha e o topete (fração da altura)")
    rd.add_argument("--largura-saida", type=int, default=0,
                    help="reduz o vídeo para essa largura (ex.: 1080); 0 (padrão) mantém a resolução original")
    rd.add_argument("--caixinha-segundos", type=float, default=0, help="0 = o vídeo todo")
    rd.set_defaults(fn=cmd_renderizar)
    pv = sub.add_parser("previa", parents=[rd], add_help=False, conflict_handler="resolve",
                        help="gera uma imagem por vídeo para aprovar a altura da caixinha")
    pv.add_argument("--saida", default="previas")
    pv.add_argument("--sem-guia", action="store_true", help="não desenhar a linha vermelha da zona segura")
    pv.set_defaults(fn=cmd_previa)
    pl = sub.add_parser("planilha", help="monta a planilha de postagem (vídeo, legenda, data e hora)")
    pl.add_argument("--plano", default="plano.csv")
    pl.add_argument("--legendas", default=str(Path(__file__).with_name("legendas-seo-caixinhas.md")))
    pl.add_argument("--saida", default="postagens.csv")
    pl.add_argument("--inicio", help="primeiro dia, AAAA-MM-DD (padrão: amanhã)")
    pl.add_argument("--horas", default="12:00", help="horários por dia, ex.: 09:00,13:00,19:00")
    pl.set_defaults(fn=cmd_planilha)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
