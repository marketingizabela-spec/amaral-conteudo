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
3. **Prévia da altura da caixinha** (uma imagem por vídeo, rápido, sem renderizar tudo):
   `python editar.py previa --videos PASTA --caixinhas PASTA_PNGS --plano plano.csv --saida previas`
   Mostrar as imagens para a Izabela aprovar a altura. A linha vermelha é o limite da zona segura de anúncio da Meta (só na prévia).
4. **Renderizar**:
   `python editar.py renderizar --videos PASTA --caixinhas PASTA_PNGS --plano plano.csv --saida editados`
5. Entregar os `IMG_xxxx_editado.mp4`. A saída mantém a resolução original do vídeo (a Izabela não quer que o vídeo seja alterado além de corte, voz e caixinha). Só reduza com `--largura-saida 1080` se ela pedir.
6. **Planilha de postagem** (Meta Business Suite): `python editar.py planilha --plano plano.csv --saida postagens.csv --inicio AAAA-MM-DD --horas 09:00,19:00`.
   Gera uma linha por vídeo com legenda SEO (de `legendas-seo-caixinhas.md`), data e hora sugeridas.

## Padrões (ajustar conforme a Izabela pedir)
- Caixinha no **topo**, centralizada, começando a **14% da altura** (zona segura de anúncio da Meta, `--margem`), durante o vídeo todo. O tamanho **se ajusta sozinho**: o script detecta o rosto, estima onde começa o topete e faz a caixinha terminar acima dele (`--folga-cabeca`, `--largura-max`). `--largura 0.5` fixa um tamanho. O tamanho em si não importa para a Izabela; o que importa é **nunca cobrir o topete** e respeitar a margem da Meta.
- **Cores: manter as originais.** Os vídeos do iPhone são HDR (HEVC 10 bits, BT.2020 HLG). Por padrão a saída mantém isso (HEVC 10 bits, mesmas tags de cor) e a caixinha é convertida de sRGB para HLG, com o branco no nível das áreas claras do vídeo (`--brilho-caixinha`, padrão 1.35). `--sdr` converte para SDR/H.264 (compatibilidade máxima e caixinha branca pura, mas as cores do vídeo mudam). Vídeos já SDR saem em H.264 com matriz BT.709.
- Resolução original (não redimensionar sem ela pedir).
- O vídeo começa 0,8 s antes da 1ª fala do apresentador (`--folga-inicio`), e o silenciamento da voz da Izabela nunca invade a fala do apresentador.
- A separação de vozes assume que **a Izabela fala primeiro**. Se errar, usar `--voz-minha amostra.wav` (áudio curto só com a voz dela) ou ajustar `--limiar`.
- Se não houver transcrição (sem internet para o Whisper), preencher o `plano.csv` à mão e usar só `renderizar`.

## Rodar no PC (Windows)
Python 3.11 + ffmpeg + `pip install faster-whisper resemblyzer rapidfuzz "opencv-python-headless<5"` (o OpenCV 5 não tem o detector de rosto usado; manter a versão 4). Os comandos acima valem a partir dessa pasta.

## Histórico de ajustes
Registre aqui o que deu errado e o que foi corrigido, para a próxima edição já sair melhor.
- 2026-10-29: `renderizar` testado com um clipe real de iPhone (4K HEVC, girado, 2 s): saiu 1080x1920, caixinha no topo, áudio ok. `analisar` (Whisper + separação de vozes) **ainda não foi testado**: o Hugging Face estava bloqueado no ambiente.
- 2026-10-30: a caixinha a 90% da largura cobria a cabeça do apresentador nos vídeos reais; padrão passou para 55% e margem 5%. O corte estava entrando em cima da 1ª fala; folga de início subiu de 0,4 s para 0,8 s.
- 2026-10-30: pedido da Izabela: a caixinha não pode cortar o topete e deve respeitar a margem de anúncio da Meta (topo 14%). Implementado ajuste automático por detecção de rosto + comando `previa` para aprovar a altura. Testado em 1 clipe (topete real a ~31%, caixinha termina a ~25%). A detecção por rosto pode falhar com cabeça inclinada, de lado ou de óculos; se não achar rosto, usa 55% e avisa.
- 2026-10-30: Izabela pediu para manter as cores. O vídeo de teste é HLG; a saída mantém tags e valores (diferença < 1/255 nas regiões sem caixinha). Sem conversão, o branco sRGB da caixinha ficava no pico do HDR (estourado) e a matriz BT.601 do ffmpeg alterava as cores. Não foi possível ver em um celular HDR; pedir para a Izabela conferir 1 vídeo no celular. Se a caixinha parecer cinza: subir `--brilho-caixinha`; se ofuscar: descer; se o Meta Business Suite recusar HEVC: usar `--sdr`.
- (pendente) O script ainda não foi testado com os vídeos longos reais. No primeiro teste, anotar: cortes certos? vozes trocadas? caixinha certa? posição/tamanho bons?
