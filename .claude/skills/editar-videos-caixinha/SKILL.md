---
name: editar-videos-caixinha
description: Edita vídeos em lote para o Instagram. Corta o início, remove a voz do entrevistador (Izabela) deixando só a fala do apresentador, e sobrepõe a caixinha de pergunta (PNG transparente) correspondente ao vídeo. Use quando pedirem para editar, cortar ou colocar a caixinha de pergunta em vídeos IMG_xxxx.MOV.
---

# Editar vídeos com caixinha de pergunta

Script: `editar.py` (nesta pasta). Texto das 50 perguntas: `caixinhas.json`.
As imagens `01.png` a `50.png` (fundo transparente, 1612x638) vêm num zip enviado pela Izabela e **não** ficam no repositório.

## Antes de começar
1. Confirme que os vídeos e as caixinhas estão acessíveis. Esta sessão pode não ver o computador da Izabela, e o conector do Google Drive só baixa arquivos de até 10 MB. Se os arquivos não chegarem, diga isso em vez de improvisar, e ofereça rodar o script no PC dela (ver "Rodar no PC").
2. Comece por **um** vídeo curto. Só processe o lote depois que ela aprovar o resultado.

## Fluxo
1. **Analisar** (transcreve, separa as vozes, sugere o corte e a caixinha):
   `python editar.py analisar --videos PASTA --plano plano.csv`
2. **Mostrar o `plano.csv` para a Izabela** e esperar a conferência. Colunas: `arquivo; inicio; silenciar; caixinha; pergunta_detectada; confianca; revisar`.
   - `inicio`: onde o vídeo passa a começar (1ª fala do apresentador menos 0,4 s).
   - `silenciar`: trechos `a-b|c-d` em que a voz dela é zerada. O vídeo continua, ficam só os espaços vazios.
   - `caixinha`: número 1 a 50, escolhido pela semelhança entre a pergunta falada e o texto das caixinhas.
   - Linhas com `CONFERIR CAIXINHA` ou `caixinha repetida` precisam de olhar humano.
3. **Renderizar**:
   `python editar.py renderizar --videos PASTA --caixinhas PASTA_PNGS --plano plano.csv --saida editados`
4. Entregar os `IMG_xxxx_editado.mp4`.

## Padrões (ajustar conforme a Izabela pedir)
- Caixinha no **topo**, centralizada, 90% da largura, durante o vídeo todo (`--posicao`, `--largura`, `--margem`, `--caixinha-segundos`).
- Saída H.264/AAC, CRF 18.
- A separação de vozes assume que **a Izabela fala primeiro**. Se errar, usar `--voz-minha amostra.wav` (áudio curto só com a voz dela) ou ajustar `--limiar`.
- Se não houver transcrição (sem internet para o Whisper), preencher o `plano.csv` à mão e usar só `renderizar`.

## Rodar no PC (Windows)
Python 3.11 + ffmpeg + `pip install faster-whisper resemblyzer rapidfuzz`. Os comandos acima valem a partir dessa pasta.

## Histórico de ajustes
Registre aqui o que deu errado e o que foi corrigido, para a próxima edição já sair melhor.
- (vazio) O script ainda não foi testado com os vídeos reais. No primeiro teste, anotar: cortes certos? vozes trocadas? caixinha certa? posição/tamanho bons?
