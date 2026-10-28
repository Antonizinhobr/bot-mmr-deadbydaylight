# DBD Coach · Worker (O Olho da Entidade)

**Um arquivo só (`worker.py`)** que fica rodando e faz tudo sozinho:

```
site (index.js) ──upload──▶ disco do site (ou R2) ──▶ worker.py ──▶ Postgres (MMR, histórico)
                                                          └────────▶ Discord (embed no canal)
```

Funciona para **sobrevivente e assassino**, com o jogo em **português ou inglês**. O worker descobre sozinho quem gravou e em que idioma, pelos textos do feed de pontuação. A tela de resultado do fim da partida confirma.

Para cada vídeo novo na fila, ele:

1. **Pega o vídeo da fila** e marca como `processando/`, para não pegar duas vezes.
2. **Valida o envio:** nome no padrão do index.js, ID do Discord válido, vídeo repetido (hash SHA-256) e limite de partidas por dia.
3. **Visão (a "IA"):** lê a HUD do Dead by Daylight frame a frame. Detalhes na tabela abaixo.
4. **Coach:** cruza os dados.
   - **Sobrevivente:** "chase longa, mas sustentada por pallets", "pallet sem stun", "caiu com chases curtas", "possível sedex", "segurou o Killer enquanto o time fazia gen".
   - **Assassino:** "ganchos concentrados em 2 sobreviventes (tunnel)", "o time fez 3 gens antes do seu 1º gancho", "chases longas", "poder pouco certeiro", "Sede de Sangue em loop".
5. **MMR:** Elo/Glicko com incerteza (RD), **separado por papel** (MMR de sobrevivente e MMR de assassino). Ele mistura o resultado com a nota técnica e pesa pela confiança da leitura.
6. **Grava no Postgres** e **posta o embed** no canal pelo webhook.
7. **Destaques:** corta até 3 clipes dos melhores momentos e posta logo depois do relatório, como vídeo tocável no Discord.
   - **Sobrevivente:** as chases mais fortes (stuns, fast vaults, Dead Hard, cegada, despistou o Killer), hit de proteção, resgate e fuga.
   - **Assassino:** insta-chases (chase curta até a queda), quedas direto com o poder e o último sacrifício de um 4K.
8. **Apaga o vídeo.** Se o vídeo foi recusado, ele fica marcado como falha, com o motivo (no modo site, apagado sozinho depois de 3 dias).

## Estrutura

```
python-worker/
├─ worker.py            ← tudo (fila, visão, coach, MMR, banco, Discord)
├─ templates/           ← imagens que a visão procura na HUD (não apague)
│   ├─ score/                  pop-ups antigos (inglês)
│   ├─ sobrevivente/en|pt/     pop-ups de sobrevivente por idioma
│   ├─ assassino/en|pt/        pop-ups de assassino por idioma (inclui poderes)
│   ├─ comum/placar/           ícones do placar (escapou, sacrificado, morto, vivo...)
│   └─ acao/ status/ generator/ survivor/
├─ .env                 ← suas chaves (ou use o .env do bot, na pasta de cima)
├─ requirements.txt
├─ iniciar_worker.bat   ← Windows: dois cliques e ele fica rodando
├─ Dockerfile / dbd-worker.service  ← para a VPS
```

> O `worker.py` precisa ficar **fora** da pasta `templates/`, do lado dela, como mostrado acima.

## Vídeos no próprio site (sem R2)

Com `ARMAZENAMENTO=site` no `.env` dos dois lados, o vídeo vai do navegador direto para o disco do app do site, e o worker baixa de lá por HTTP. O `mmr.html` não muda: ele continua pedindo o link em `/api/gerar-link` e mandando o vídeo com `PUT`, só que o link agora aponta para o próprio site.

```
videos/<id do jogador>/<envio>/
    meta.json        ficha do jogador (nome, foto, ID do Discord)
    pendente.mp4     esperando o worker   ->  processando.mp4  ->  apagado no fim
    falha.mp4        recusado (fica 3 dias para a staff conferir, depois some)
    estado.json      tentativas e motivo
```

- **.env do index.js:** `WORKER_TOKEN=<40+ caracteres aleatórios>`. O endereço `https://botmmr.discloud.dev` já vem configurado (`API_URL_PUBLICA` só se mudar de domínio). Sem as variáveis `R2_*`, os dois lados já usam o modo site sozinhos. Opcionais: `PASTA_VIDEOS`, `GUARDAR_FALHAS_DIAS` (padrão 3), `MAX_TAMANHO_GB`.
- **.env do worker:** o **mesmo** `WORKER_TOKEN`. O endereço `https://botmmr.discloud.dev` já vem configurado (`SITE_URL` só se mudar de domínio).
- O link de upload vale 1 hora, só serve uma vez e só aceita o tamanho declarado. Envio cortado no meio não entra na fila.
- As rotas `/worker/...` só respondem para quem tem o `WORKER_TOKEN`.
- Se o download do worker cair no meio, ele continua de onde parou.
- Não apague nem reenvie o app do site do zero com vídeos na fila: a pasta `videos/` vai junto. Atualizar arquivos (commit) não mexe nela.

## Instalação no Windows (no seu PC)

1. Instale o **Python 3.12** em python.org e marque a opção "Add to PATH".
2. Instale o ffmpeg e o Tesseract. Abra o PowerShell e rode:
   ```
   winget install Gyan.FFmpeg
   winget install UB-Mannheim.TesseractOCR
   ```
   No instalador do Tesseract, marque o idioma **Portuguese** (em "Additional language data"). Ele lê a tela de resultado, o nome do mapa, os rótulos em português e o contador de geradores. Sem ele, o worker funciona, mas perde essas leituras.
3. Instale as dependências, dentro da pasta `python-worker`:
   ```
   pip install -r requirements.txt
   ```
4. Configure o `.env`. Copie `.env.example` para `.env` e preencha. O worker também lê o `.env` da pasta de cima, o mesmo do bot. Os nomes `R2_*` e `DISCORD_TOKEN` são os mesmos do index.js, então basta acrescentar:
   ```
   DATABASE_URL=postgresql://...
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
   ```
5. Teste a configuração:
   ```
   python worker.py --checar          # confere .env, banco, fila, ffmpeg e tesseract
   python worker.py --teste-discord   # manda um embed de teste no canal
   ```
6. Rode o worker: dê dois cliques em `iniciar_worker.bat`, ou rode `python worker.py`. Se ele cair, o `.bat` reinicia sozinho.

## Hospedar na Discloud

O worker roda como um **app separado** do bot/site (index.js), do tipo `bot`.

1. Coloque o `.env` dentro da pasta `python-worker`, ao lado do `worker.py`, com `ARMAZENAMENTO=site`, `SITE_URL`, `WORKER_TOKEN` (ou as chaves do R2), `DATABASE_URL` (o Postgres da Discloud) e `DISCORD_WEBHOOK_URL`. Se o plano tiver pouco disco, limite o tamanho:
   ```
   MAX_TAMANHO_GB=2
   ```
   O `PROCESSOS` não precisa: o worker calcula sozinho pela RAM do plano (uns 450 MB por processo).
2. O `discloud.config` já vem pronto:
   ```
   NAME=dbd-coach-worker
   TYPE=bot
   MAIN=worker.py
   RAM=2048
   VERSION=latest
   APT=ffmpeg, tesseract, libgl
   AUTORESTART=true
   ```
   - `RAM`: use o máximo que o seu plano deixar. Com 1024 MB roda 1 processo, mais devagar. Com 2048 MB, 2 processos.
   - `AUTORESTART` só funciona do plano Platinum para cima. Se o seu plano não tiver, apague essa linha.
   - O Tesseract da Discloud vem só com inglês. O português já está na pasta `tessdata/` do pacote, e o worker usa sozinho.
3. Compacte o **conteúdo** da pasta `python-worker` num .zip. O `worker.py`, o `discloud.config`, o `.env`, o `requirements.txt` e as pastas `templates/` e `tessdata/` têm que ficar na raiz do zip, sem uma subpasta por cima.
4. Suba o zip pelo painel da Discloud ou pelo bot dela, com `.upload`. Ela instala o `requirements.txt` sozinha.
5. No terminal/logs do app deve aparecer "O OLHO DA ENTIDADE ESTÁ ATIVO". A partir daí, ele pega sozinho os vídeos que o site recebe.

**Atualizar depois:** suba o zip novo com o mesmo app (commit/update no painel). O banco e os vídeos na fila não mudam.

**Pontos de atenção na Discloud:**
- **CPU compartilhada:** uma partida de 10–15 min leva de 5 a 15 min para analisar. Os vídeos esperam na fila e são processados um por vez.
- **Espaço em disco:** o vídeo é baixado para a pasta `_tmp` do worker e apagado no fim (no modo site ele também ocupa o disco do site até a análise terminar).
- **Se o app reiniciar no meio** de um vídeo, ele volta sozinho para a fila depois de uns 90 min (recuperação de órfãos).

## Instalação na VPS (Ubuntu)

```bash
sudo apt update && sudo apt install -y python3-venv ffmpeg tesseract-ocr tesseract-ocr-por
sudo useradd -m dbd && sudo mkdir -p /opt/python-worker && sudo chown dbd /opt/python-worker
# copie worker.py, templates/, requirements.txt e .env para /opt/python-worker
cd /opt/python-worker && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python worker.py --checar
sudo cp dbd-worker.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now dbd-worker
journalctl -u dbd-worker -f      # log ao vivo
```

Também dá para usar Docker: `docker build -t dbd-worker . && docker run -d --restart=always --env-file .env dbd-worker`.

**Desempenho:** 1 hora de vídeo leva uns **25 min em 2 núcleos**. Em 4 núcleos, uns 12 min, e em 8 núcleos, uns 6 min. O worker usa os núcleos da máquina menos 1; para mudar, ajuste `PROCESSOS`.

## Comandos

| Comando | O que faz |
|---|---|
| `python worker.py` | Fica rodando e olhando a fila a cada 10 s |
| `python worker.py --uma-vez` | Processa o que tem na fila e sai |
| `python worker.py --local partida.mp4 --jogador <ID>` | Só analisa um arquivo seu e mostra o relatório. Não grava nada |
| `python worker.py --local partida.mp4 --jogador <ID> --gravar` | Analisa, grava o MMR e posta no Discord |
| `python worker.py --local partida.mp4 --jogador <ID> --destaques clipes` | Analisa e salva os clipes de destaque na pasta `clipes`, sem postar |
| `python worker.py --checar` | Confere configuração e conexões |
| `python worker.py --teste-discord` | Embed de teste no canal |

Para parar, aperte `Ctrl+C` uma vez: ele termina o vídeo atual e sai. Apertando duas vezes, ele sai na hora.

## O que a visão lê na HUD

| Fonte na tela | Sobrevivente | Assassino |
|---|---|---|
| Feed de pontuação (sup. direito) | PALLET DROP, KILLER STUN, VAULT, DASH DODGER, KILLER BLIND, ESCAPED, PROTECTION, HOOK RESCUE, GREAT/GOOD SKILL CHECK, COOP, REPAIRS, ASSIST... | HIT/ATAQUE, HOOKED/GANCHO (todo gancho), CAUGHT/ENGANCHADO (1º gancho de cada um), DAMAGE GENERATOR, DESTRUCTION, SACRIFICE, KILL, BLOODLUST e os pop-ups de poder (Lethal Rush, Speared, motosserra, Ardil Onírico, feitiços do Lich...) |
| Colchetes `( )` nos retratos | Sua chase e as dos colegas | Qual sobrevivente você está perseguindo e por quanto tempo |
| Barra de ação | RECOVER, WIGGLE, REPAIR, HEAL, UNHOOK, OPEN ESCAPE (inglês e português) | — |
| Contador de geradores | Gens que o time fez | Gens que faltam (pressão) |
| Losangos de perk | HUD na tela (confiança da leitura), mesmo com overlay de teclado no canto | idem |
| Nome do mapa (inf. esquerdo) | Começo da partida | idem |
| Tela de resultado + placar | ESCAPED / SACRIFICED / KILLED | Brutal / Ruthless / Merciless = kills; ícones dos 4 sobreviventes; etiqueta **BOT** |

A **escala da HUD é detectada sozinha** (de 0,70x a 1,60x), então funciona com qualquer configuração de "HUD scale" do jogo. A resolução pode ser qualquer uma em 16:9.

**Partida personalizada contra bots** (todos os outros jogadores com a etiqueta BOT no placar): o coach roda, mas o MMR não muda. Se só alguns jogadores viraram bot (caíram da partida), ela conta com peso menor.

## O MMR

- **E** é a sua expectativa contra o lobby médio, que é o MMR médio dos jogadores com 5 ou mais partidas.
- **U** é a incerteza. Conta nova mexe mais (RD 350 dá 1,5x) e conta estável mexe menos (RD 50 dá 0,5x). Se o jogador fica parado, o RD volta a crescer, como no Glicko.
- **S** é o resultado combinado: 30% fuga e 70% nota técnica. Se o coach detectar sacrifício estratégico, a fuga passa a pesar só 10%.
- **Os pesos da nota se ajustam pela função na partida.** Quem passou a partida em chase não podia estar no gerador, então a Chase pesa mais e Objetivo/Equipe pesam menos.
- **C** é a confiança da leitura (quanto da HUD foi reconhecida). Partida incompleta vale metade.
- **Δ = K · (S − E) · U · C**, com limite de ±50 por partida.
- **Assassino:** S = 35% kills (0 a 4) + 65% nota técnica. As categorias da nota são Letalidade, Pressão, Chase, Espalhamento, Poder e Decisão.

## Banco de dados

O worker **migra sozinho** as tabelas que você já tem. Ele só **adiciona** colunas (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`) e nunca apaga dados. O histórico passa a guardar scores por categoria, métricas, achados do coach, o cálculo do MMR, o hash do vídeo, o mapa e o resultado.

O worker antigo tinha um bug que criava um jogador novo a cada partida, com ID no formato `ID_timestamp`. Para ver esses registros:

```sql
SELECT * FROM jogadores WHERE id_discord LIKE '%\_%';
```

> **Supabase:** se a conexão falhar com erro de host ou IPv6, use a string do **Session pooler** em Project Settings → Database. Ela usa um host `*.pooler.supabase.com`.

## Segurança

- **Nenhuma chave fica no código.** Tudo vem do `.env`. Nunca suba o `.env` para o Git.
- As chaves do R2 e a senha do banco do worker antigo apareceram em texto. **Gere chaves novas do R2 e troque a senha do Supabase.**
- Trate o link do webhook como senha: com ele, qualquer pessoa posta no canal. Se vazar, gere outro nas configurações do canal.
- **Identidade do jogador:** o index.js confere o login do Firebase do site (token assinado pelo Google) e usa o `uid` que vem dele, nunca o que o navegador manda. Ele também grava uma ficha `meta/<vídeo>.json` com o nome e a foto do jogador. O worker lê essa ficha para montar o embed e apaga depois. Se o vídeo tiver um ID do Discord, ou se a ficha tiver `discord_id`, o embed marca o jogador; senão, mostra o nome.

## Destaques (clipes)

- Cada clipe tem até 40 s, em 720p, H.264 com áudio, e no máximo 9,5 MB. O Discord aceita até 10 MB por arquivo num servidor sem boost. Se o servidor tiver boost, dá para aumentar `HIGHLIGHT_MAX_MB`.
- Cortar os clipes leva uns 30 s a 1 min por partida. Para desligar, coloque `HIGHLIGHTS=0` no `.env`.
- Os clipes são cortados do vídeo original enquanto ele ainda está no worker. Depois de postar, tudo é apagado.

## Limitações conhecidas

- **Grave até a tela de resultado e o placar** (uns 5 s depois do emblema). É dali que sai o resultado oficial. Sem ela, o worker usa o feed (SURVIVED, SACRIFICE...), que é menos exato.
- **Poderes de assassino:** o worker conhece os pop-ups dos assassinos já gravados (Blight, Deathslinger, Lich, Canibal, Freddy). Nos outros, conta hits, ganchos, chutes e chases normalmente, mas não mede a precisão do poder. Para ensinar um assassino novo, grave uma partida e recorte os textos do feed para `templates/assassino/<idioma>/score`.
- **Os limites de "alto/baixo" do coach** são valores iniciais. Com umas 100 partidas no banco, vale trocar pelos percentis reais.
