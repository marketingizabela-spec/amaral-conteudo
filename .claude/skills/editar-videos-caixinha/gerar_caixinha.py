"""Gera caixinhas no padrão das originais: reaproveita cabeçalho/fundo do 01.png e escreve a pergunta."""
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONTE = "/usr/share/fonts/opentype/inter/Inter-Regular.otf"  # no Windows: C:/Windows/Fonts/Inter-Regular.ttf (instalar a Inter)
COR = (0x26, 0x2a, 0x30, 255)
CAB = 218       # fim do cabeçalho "Faça uma pergunta"
PASSO = 101     # distância entre as linhas da pergunta
BASE_L1 = 400   # linha de base da 1ª linha (igual com 1 ou 2 linhas)


def base(modelo):
    a = np.array(Image.open(modelo).convert("RGBA"))
    corpo = a[CAB:]
    corpo[corpo[:, :, 3] > 0, :3] = 255   # apaga a pergunta antiga, mantém os cantos arredondados
    return Image.fromarray(a)


def quebra(txt, f, maxw):
    linhas, atual = [], ""
    for p in txt.split():
        t = (atual + " " + p).strip()
        if f.getlength(t) <= maxw or not atual:
            atual = t
        else:
            linhas.append(atual)
            atual = p
    linhas.append(atual)
    # como no Instagram: a última linha nunca fica com uma palavra sozinha
    # (só desce a palavra anterior se ela for curta, até 5 letras, como nas originais)
    if len(linhas) > 1 and len(linhas[-1].split()) == 1 and len(linhas[-2].split()) > 1:
        *resto, ultima = linhas[-2].split()
        if len(ultima) <= 5:
            linhas[-2:] = [" ".join(resto), ultima + " " + linhas[-1]]
    return linhas


def desenha(modelos, txt, tam, maxw):
    """modelos = {1: png de 1 linha (536 px), 2: png de 2 linhas (638 px)}"""
    f = ImageFont.truetype(FONTE, tam)
    linhas = quebra(txt, f, maxw)
    while len(linhas) > 2 and tam > 60:   # nunca 3 linhas: diminui a fonte
        tam -= 2
        f = ImageFont.truetype(FONTE, tam)
        linhas = quebra(txt, f, maxw)
    im = base(modelos[len(linhas)])
    d = ImageDraw.Draw(im)
    for i, l in enumerate(linhas):
        d.text((im.width / 2, BASE_L1 + i * PASSO), l, font=f, fill=COR, anchor="ms")
    return im, linhas, tam


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    pasta = Path(__file__).with_name("caixinhas-novas")
    p = argparse.ArgumentParser(description="Gera uma caixinha 'Faça uma pergunta' igual às originais.")
    p.add_argument("pergunta")
    p.add_argument("saida", help="arquivo .png de saída")
    a = p.parse_args()
    modelos = {1: pasta / "_modelo_1linha.png", 2: pasta / "_modelo_2linhas.png"}
    img, linhas, tam = desenha(modelos, a.pergunta, 80, 1450)
    img.save(a.saida)
    print(f"{a.saida}: {linhas} (fonte {tam})")
