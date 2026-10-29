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
PAD_CORTE = 0.4       # segundos antes da 1ª fala do apresentador
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
        inicio = max(0.0, segs[primeiro][0] - PAD_CORTE)
        silenciar = [(max(inicio, s - PAD_SILENCIO), e + PAD_SILENCIO)
                     for (s, e, _), m in zip(segs, meu) if m and e > inicio]
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
def montar_filtro(w, reduzir, silenciar_rel, a):
    """w = largura do vídeo de saída; reduzir=True escala o vídeo para essa largura."""
    larg = int(w * a.largura) // 2 * 2
    if a.posicao == "topo":
        y = f"main_h*{a.margem}"
    elif a.posicao == "baixo":
        y = f"main_h-overlay_h-main_h*{a.margem}"
    else:
        y = "(main_h-overlay_h)/2"
    enable = f":enable='between(t,0,{a.caixinha_segundos})'" if a.caixinha_segundos > 0 else ""
    if silenciar_rel:
        expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in silenciar_rel)
        audio = f"[0:a]volume=0:enable='{expr}'[a]"
    else:
        audio = "[0:a]anull[a]"
    base = "[base]" if reduzir else "[0:v]"
    pre = f"[0:v]scale={w}:-2[base];" if reduzir else ""
    return (f"{audio};{pre}[1:v]scale={larg}:-1,format=rgba[c];"
            f"{base}[c]overlay=x=(main_w-overlay_w)/2:y={y}:shortest=1{enable}[v]")


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
        w, h = probe_dims(video)
        reduzir = bool(a.largura_saida) and w > a.largura_saida
        if reduzir:
            w = a.largura_saida // 2 * 2
        script = saida / f".{video.stem}.filtro.txt"
        script.write_text(montar_filtro(w, reduzir, sil, a), encoding="utf-8")
        destino = saida / f"{video.stem}_editado.mp4"
        print(f"Renderizando {nome} -> {destino.name} (caixinha {int(r['caixinha']):02d})", flush=True)
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{inicio:.3f}", "-i", str(video),
             "-loop", "1", "-framerate", "30", "-i", str(png),
             "-filter_complex_script", str(script), "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(destino)])
        script.unlink(missing_ok=True)
    print("Pronto.")


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
    an.add_argument("--limiar", type=float, default=0.75, help="similaridade de voz (0-1) para ser 'eu'")
    an.set_defaults(fn=cmd_analisar)
    rd = sub.add_parser("renderizar", help="gera os vídeos a partir do plano.csv")
    rd.add_argument("--videos", required=True)
    rd.add_argument("--caixinhas", required=True, help="pasta com 01.png ... 50.png")
    rd.add_argument("--plano", default="plano.csv")
    rd.add_argument("--saida", default="saida")
    rd.add_argument("--posicao", choices=["topo", "centro", "baixo"], default="topo")
    rd.add_argument("--largura", type=float, default=0.9, help="largura da caixinha (fração do vídeo)")
    rd.add_argument("--margem", type=float, default=0.08, help="distância da borda (fração da altura)")
    rd.add_argument("--largura-saida", type=int, default=0,
                    help="reduz o vídeo para essa largura (ex.: 1080); 0 (padrão) mantém a resolução original")
    rd.add_argument("--caixinha-segundos", type=float, default=0, help="0 = o vídeo todo")
    rd.set_defaults(fn=cmd_renderizar)
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
