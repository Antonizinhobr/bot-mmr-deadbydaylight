# 🩸 Refúgio da Névoa · Community MMR & DBD Coach

Sistema de **MMR comunitário** e **coach automático** para *Dead by Daylight*.

O jogador envia a gravação de uma partida pelo site. Um worker analisa o vídeo frame a frame, como se assistisse à partida, e depois:

- mede o desempenho dele;
- calcula o MMR;
- posta no Discord um relatório de coach com os clipes dos melhores momentos.

Funciona para **sobrevivente e assassino**, com o jogo em **português ou inglês**. Não usa nenhuma API da Behaviour: tudo sai da própria imagem da partida.

> Projeto da comunidade. **Não é afiliado à Behaviour Interactive nem a Dead by Daylight™.**

---

## Sumário

1. [Como funciona](#como-funciona)
2. [O que o sistema faz](#o-que-o-sistema-faz)
3. [Estrutura do repositório](#estrutura-do-repositório)
4. [Requisitos](#requisitos)
5. [Configuração](#configuração)
6. [Hospedagem na Discloud](#hospedagem-na-discloud)
7. [Rodar no seu PC ou numa VPS](#rodar-no-seu-pc-ou-numa-vps)
8. [Integrar a página de envio](#integrar-a-página-de-envio)
9. [Referência da API](#referência-da-api)
10. [Onde ficam os vídeos](#onde-ficam-os-vídeos)
11. [Como a análise funciona](#como-a-análise-funciona)
12. [Coach](#coach)
13. [Cálculo do MMR](#cálculo-do-mmr)
14. [Banco de dados](#banco-de-dados)
15. [Destaques (clipes)](#destaques-clipes)
16. [Comandos do worker](#comandos-do-worker)
17. [Segurança](#segurança)
18. [Solução de problemas](#solução-de-problemas)
19. [Limitações e próximos passos](#limitações-e-próximos-passos)

---

## Como funciona

```
 Navegador (página de envio)
   │  1. login (Firebase) ─────────────────────────────┐
   │  2. POST /api/gerar-link  (token do Firebase)     │
   │  3. PUT  /api/upload/...  (vídeo em pedaços)      ▼
   │                                            ┌──────────────┐
   └──────────────────────────────────────────▶ │   bot-mmr    │  Node.js (Express + discord.js)
                                                │  index.js    │  valida o login, recebe e guarda o vídeo
                                                └──────┬───────┘
                                    videos/<jogador>/<envio>/pendente.mp4
                                                       │  /worker/... (token secreto)
                                                ┌──────▼───────┐
                                                │    worker    │  Python (OpenCV + Tesseract + ffmpeg)
                                                │  worker.py   │  visão → coach → MMR → destaques
                                                └──┬────────┬──┘
                                 PostgreSQL ◀──────┘        └──────▶ Discord (webhook)
                          (jogadores, histórico)              relatório + clipes no canal
```

São **dois programas**, hospedados separados:

| App | Linguagem | Papel |
|---|---|---|
| **bot-mmr** (`index.js`, na raiz) | Node.js | API pública. Confere o login do jogador, gera links de envio, recebe o vídeo, guarda no disco e entrega ao worker. Também mantém um bot do Discord online. |
| **worker** (`python-worker/worker.py`) | Python | Pega os vídeos da fila, analisa, calcula o MMR, grava no banco, posta no Discord e apaga o vídeo. |

---

## O que o sistema faz

### bot-mmr (API)

- **Login seguro.** Confere o *ID token* do Firebase com as chaves públicas do Google: RS256, `aud`/`iss` do projeto e validade. O ID do jogador sempre sai do token assinado, nunca do que o navegador manda.
- **Login pelo Discord.** Se o login for pelo Discord, o ID do Discord é encontrado no token (`discord_id`, `discordId` ou um `uid` no formato `discord:<id>`) e usado para marcar o jogador no relatório.
- **Link de envio assinado** (HMAC). Ele:
  - vale por 6 horas;
  - serve uma vez só;
  - fica preso ao tamanho declarado do arquivo.
- **Envio em pedaços de 16 MB.** Isso evita o limite de tamanho dos proxies de hospedagem. O envio é retomável: se a conexão cair, continua de onde parou.
- **Anti-abuso:**
  - limite de envios por hora por jogador;
  - limite de tamanho;
  - só aceita `.mp4`, `.mov`, `.mkv` e `.webm`;
  - nomes de arquivo sanitizados (sem `../`).
- **Fila em pastas** (`pendente` → `processando` → apagado ou `falha`). A troca de estado é atômica, então dois workers nunca pegam o mesmo vídeo.
- **Faxina automática** a cada hora. Ela apaga:
  - envios abandonados;
  - pedaços de envio interrompido;
  - falhas antigas.
- **Modo R2 opcional.** Em vez do disco, pode usar o Cloudflare R2 com links pré-assinados.

### worker (análise)

- **Descobre sozinho**:
  - o papel (sobrevivente ou assassino);
  - o idioma do jogo (PT ou EN);
  - a escala da HUD (0,70x a 1,60x).
- **Lê a HUD**:
  - o feed de pontuação;
  - a barra de ação;
  - os status;
  - o contador de geradores;
  - os colchetes de chase;
  - o nome do mapa;
  - o emblema do fim da partida;
  - o placar.
- **Separa várias partidas** num mesmo vídeo.
- **Detecta partidas contra bots e personalizadas.** Personalizada só com bots roda o coach, mas não mexe no MMR.
- **Coach** com nota por categoria e achados em texto, como "chase longa, mas sustentada por pallets" ou "ganchos concentrados em 2 sobreviventes".
- **MMR estilo Elo/Glicko**:
  - com incerteza (RD);
  - **separado por papel**;
  - pesado pela confiança da leitura.
- **Relatório no Discord** (embed). Marca o jogador e mostra MMR, notas, achados, mapa e resultado.
- **Destaques.** Corta até 3 clipes (chases, insta-downs, fugas...) e posta como vídeo tocável.
- **Anti-duplicata.** O SHA-256 de cada vídeo vai para o banco, então o mesmo arquivo não conta duas vezes.
- **Limite de partidas por jogador em 24 h.**
- **Recuperação automática.** Se o worker cair no meio, o vídeo volta para a fila. Depois de 3 tentativas, vai para falha.

---

## Estrutura do repositório

O repositório é a própria pasta do **bot-mmr**. O worker fica numa subpasta, `python-worker/`:

```
bot-mmr/
├─ index.js                  ← API + bot do Discord (Discloud: TYPE=site)
├─ package.json
├─ package-lock.json
├─ discloud.config           ← configuração do app do site
├─ .env                      ← suas chaves do site (NÃO vai para o GitHub)
├─ .env.example              ← modelo do .env do site (vai para o GitHub)
├─ .gitignore
├─ README.md                 ← este arquivo
├─ node_modules/             ← criado pelo npm install (NÃO vai para o GitHub)
└─ python-worker/            ← o worker (Discloud: app separado, TYPE=bot)
   ├─ worker.py
   ├─ requirements.txt
   ├─ discloud.config        ← configuração do app do worker
   ├─ .env                   ← suas chaves do worker (NÃO vai para o GitHub)
   ├─ .env.example           ← modelo do .env do worker (vai para o GitHub)
   ├─ README.md              ← guia rápido do worker
   ├─ templates/             ← imagens que a visão procura na HUD (não apague)
   │  ├─ sobrevivente/en|pt/score/   pop-ups de sobrevivente por idioma
   │  ├─ assassino/en|pt/score/      pop-ups de assassino por idioma (inclui poderes)
   │  ├─ comum/placar/               ícones do placar (escapou, sacrificado, bot...)
   │  └─ score/ acao/ status/ generator/ survivor/
   └─ tessdata/              ← idiomas do OCR (eng + por), usados automaticamente
```

> O `.env` **nunca** vai para o repositório. O `.gitignore` bloqueia os dois `.env`, a `node_modules/`, a pasta `videos/`, a `_tmp/` do worker e os logs. Os `.env.example` vão no lugar deles, só com os nomes das variáveis.

> O `worker.py` lê primeiro o `python-worker/.env` e depois o `.env` da pasta de cima (o do site). O que estiver nos dois vale o do worker. Assim, variáveis repetidas, como `WORKER_TOKEN` e `DISCORD_TOKEN`, podem ficar só no `.env` do site quando você roda tudo no mesmo PC. Na Discloud, os apps são separados, então cada um precisa do seu `.env` completo.

---

## Requisitos

| | bot-mmr | worker |
|---|---|---|
| Runtime | Node.js 18+ | Python 3.10+ |
| Programas | — | `ffmpeg`, `tesseract-ocr` |
| RAM | 512 MB | 1 GB no mínimo, 2 GB recomendado (~450 MB por processo de análise) |
| Disco | Espaço para os vídeos na fila (1 a 3 GB por partida em 1080p) | Espaço para 1 vídeo por vez (pasta `_tmp`) |

Você também vai precisar de:

- Um projeto no **Firebase Authentication** (o login do site).
- Um **bot do Discord** (token) e um **webhook** no canal onde os relatórios vão aparecer.
- Um **PostgreSQL**: o da Discloud, o do Supabase ou qualquer outro.

---

## Configuração

### 1. Gerar o `WORKER_TOKEN`

É a senha que o worker usa para falar com a API. Ela tem que ser **igual nos dois `.env`** e ter **40 caracteres aleatórios ou mais**. Para gerar:

```bash
node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
# ou
python -c "import secrets; print(secrets.token_hex(32))"
```

### 2. Discord

1. **Bot** (opcional no worker, usado pelo bot-mmr):
   - Crie em [Discord Developer Portal](https://discord.com/developers/applications) → *Bot* → *Reset Token*.
   - O worker usa o token para conferir se o ID do jogador existe e para pegar nome e avatar.
2. **Webhook:** no canal das análises, vá em *Editar canal → Integrações → Webhooks → Novo webhook → Copiar URL*.

### 3. Banco de dados

O worker **cria e atualiza as tabelas sozinho** na primeira vez que roda. Você só precisa da URL de conexão:

```
postgresql://USUARIO:SENHA@HOST:5432/NOME_DO_BANCO
```

- **Discloud:** a URL aparece no painel do banco. O host é interno e só funciona para apps que também estão na Discloud.
- **Supabase:** use a URL do **Session pooler** (*Project Settings → Database → Connection string*), com host `*.pooler.supabase.com`. O endereço direto `db.<projeto>.supabase.co` só funciona por IPv6.
- Senha com `@`, `#`, `/` ou `:` precisa de URL-encode. O mais fácil é usar uma senha só com letras e números.

### 4. `.env` do bot-mmr

Na raiz do projeto, copie `.env.example` para `.env`:

| Variável | Obrigatória | Padrão | O que é |
|---|---|---|---|
| `WORKER_TOKEN` | ✅ (modo site) | — | Segredo compartilhado com o worker |
| `DISCORD_TOKEN` | ✅ | — | Token do bot do Discord |
| `FIREBASE_PROJECT_ID` | ✅ | — | O `projectId` do seu Firebase |
| `API_URL_PUBLICA` | recomendado | domínio do projeto | Endereço público da API, ex.: `https://seu-app.discloud.dev`. Os links de envio são montados com ele |
| `ALLOWED_ORIGINS` | recomendado | todas | Sites que podem chamar a API, separados por vírgula. Ex.: `https://seusite.com.br,https://www.seusite.com.br` |
| `ARMAZENAMENTO` | — | `site` (`r2` se houver `R2_BUCKET_NAME`) | `site` = disco do app · `r2` = Cloudflare R2 |
| `PASTA_VIDEOS` | — | `./videos` | Onde os vídeos ficam no modo site |
| `MAX_TAMANHO_GB` | — | `5` | Tamanho máximo de um vídeo |
| `LINKS_POR_HORA` | — | `6` | Envios por jogador por hora |
| `GUARDAR_FALHAS_DIAS` | — | `3` | Dias que um vídeo recusado fica guardado para a staff conferir |
| `PORT` | — | `8080` | Porta HTTP. Na Discloud, deixe sem definir |
| `MODO_LEGADO` | — | `0` | `1` aceita `jogadorId` no corpo, sem login. **Só para transição; deixe 0** |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` | só modo r2 | — | Credenciais do Cloudflare R2 |
| `VALIDAR_TAMANHO` | — | `1` | Modo r2: assina o tamanho no link pré-assinado |

### 5. `.env` do worker

Na pasta `python-worker`, copie `.env.example` para `.env`. Se rodar tudo no mesmo PC, o worker também aproveita o `.env` da raiz (veja a [estrutura](#estrutura-do-repositório)).

**Essenciais**

| Variável | Obrigatória | O que é |
|---|---|---|
| `WORKER_TOKEN` | ✅ (modo site) | O **mesmo** valor do bot-mmr |
| `SITE_URL` | recomendado | Endereço da API (o mesmo de `API_URL_PUBLICA`) |
| `DATABASE_URL` | ✅ | URL do PostgreSQL |
| `DISCORD_WEBHOOK_URL` | ✅ | Webhook do canal das análises |
| `DISCORD_TOKEN` | — | Token do bot. Confere o ID do jogador e pega nome e avatar |
| `ARMAZENAMENTO` | — | `site` (padrão) ou `r2`. No modo r2, defina também as variáveis `R2_*` |

**Ajustes da análise e do MMR** (todos opcionais)

| Variável | Padrão | O que faz |
|---|---|---|
| `PROCESSOS` | automático | Processos de análise em paralelo. Calculado pela RAM e pelos núcleos, até 8 |
| `FPS_AMOSTRA` | `2` | Frames analisados por segundo de vídeo |
| `CONFIANCA_MINIMA` | `0.30` | Abaixo disso o vídeo é recusado (HUD não reconhecida) |
| `DURACAO_REJEITAR` | `60` | Vídeos mais curtos que isso (em s) nem são analisados |
| `DURACAO_MINIMA` | `240` | Partidas mais curtas contam como incompletas e valem metade do MMR |
| `MAX_PARTIDAS_24H` | `15` | Partidas por jogador em 24 h |
| `MAX_TAMANHO_GB` | `6` | Tamanho máximo aceito pelo worker |
| `MMR_INICIAL` / `RD_INICIAL` | `1500` / `350` | Valores de uma conta nova |
| `K_FACTOR` | `40` | Quanto o MMR mexe por partida |
| `DELTA_MAX` | `50` | Maior ganho ou perda numa partida |
| `INTERVALO_FILA` | `10` | Segundos entre cada olhada na fila |
| `APAGAR_VIDEO` | `1` | `0` guarda o vídeo processado como falha, em vez de apagar |
| `MENCIONAR_JOGADOR` | `1` | Marca o jogador no embed |
| `EXIGIR_ID_DISCORD` | `0` | `1` só aceita envios com ID do Discord |
| `NOME_SERVIDOR` | `Refúgio da Névoa` | Nome mostrado nos embeds |
| `PASTA_TEMP` | `./_tmp` | Onde o vídeo fica durante a análise |
| `TESSERACT_CMD` | automático | Caminho do Tesseract, se não estiver no PATH |

**Destaques**

| Variável | Padrão | O que faz |
|---|---|---|
| `HIGHLIGHTS` | `1` | `0` desliga os clipes |
| `HIGHLIGHTS_MAX` | `3` | Clipes por partida |
| `HIGHLIGHT_MAX_MB` | `9.5` | Tamanho máximo de cada clipe (o Discord aceita 10 MB em servidor sem boost) |
| `HIGHLIGHT_ALTURA` | `720` | Resolução dos clipes |
| `HIGHLIGHT_SEG_MAX` | `40` | Duração máxima de cada clipe, em segundos |

---

## Hospedagem na Discloud

São **dois apps** e, se quiser, um banco PostgreSQL da própria Discloud.

### App 1: bot-mmr (site)

`discloud.config` (na raiz):

```ini
NAME=bot-mmr
TYPE=site
ID=seu-subdominio
MAIN=index.js
RAM=512
VERSION=latest
AUTORESTART=true
```

- `TYPE=site` com `ID` cria o domínio `https://seu-subdominio.discloud.dev`.
- Site na Discloud pede **plano Platinum** e **512 MB de RAM** no mínimo.
- A Discloud encaminha o domínio para a porta **8080**, que é a padrão do `index.js`. Não defina `PORT`.
- Compacte o **conteúdo** da raiz do projeto, com `.env`, `index.js`, `package.json`, `package-lock.json` e `discloud.config` na raiz do zip, e suba pelo painel.
- **Deixe de fora do zip a pasta `python-worker/` e a `node_modules/`.** O worker é outro app, e a Discloud instala as dependências do Node sozinha pelo `package.json`.
- Os vídeos ficam em `videos/` dentro do app. Atualizar os arquivos não mexe nessa pasta, mas **apagar e recriar o app apaga** os vídeos que ainda estão na fila.

### App 2: worker

`python-worker/discloud.config`:

```ini
NAME=dbd-coach-worker
TYPE=bot
MAIN=worker.py
RAM=2048
VERSION=latest
APT=ffmpeg, tesseract, libgl
AUTORESTART=true
```

- `APT` instala o ffmpeg e o Tesseract. O português do OCR já vai na pasta `tessdata/`.
- `RAM`: use o máximo que o plano deixar. Com 1024 MB roda 1 processo (mais devagar); com 2048 MB, 2 processos.
- `AUTORESTART` só funciona do plano Platinum para cima.
- Compacte o **conteúdo** da pasta `python-worker` (não a pasta em si), com `worker.py`, `discloud.config`, `.env`, `requirements.txt`, `templates/` e `tessdata/` na raiz do zip, e suba como um app novo. A Discloud instala o `requirements.txt` sozinha.
- O worker não precisa de domínio: ele só faz conexões de saída (API, banco, Discord).

No log do worker deve aparecer:

```
O OLHO DA ENTIDADE ESTÁ ATIVO · v3.6 · site https://seu-subdominio.discloud.dev · 2 processo(s) · 2.0 fps
```

### Banco

Crie um PostgreSQL no painel da Discloud e cole a URL no `DATABASE_URL` do worker. As tabelas são criadas na primeira execução.

---

## Rodar no seu PC ou numa VPS

### bot-mmr

Na raiz do projeto:

```bash
npm install
cp .env.example .env      # preencha
node index.js
```

### worker

**Windows**
1. Instale o [Python 3.10+](https://www.python.org/downloads/) (marque *Add to PATH*), o [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) (no PATH) e o [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki).
2. Na pasta `python-worker`:
   ```bat
   cd python-worker
   pip install -r requirements.txt
   copy .env.example .env
   python worker.py --checar
   python worker.py
   ```

**Ubuntu / VPS**
```bash
sudo apt install -y python3-pip ffmpeg tesseract-ocr tesseract-ocr-por libgl1
cd python-worker
pip install -r requirements.txt     # em sistemas novos: --break-system-packages ou um venv
cp .env.example .env                # preencha
python3 worker.py --checar
python3 worker.py
```

Para manter rodando numa VPS, use `systemd`, `pm2` ou `screen`. O worker para com segurança no `Ctrl+C` / `SIGTERM`: termina o vídeo atual e sai.

---

## Integrar a página de envio

A página só precisa de duas chamadas. O jogador tem que estar logado no Firebase.

```js
const API = 'https://seu-subdominio.discloud.dev';

const token = await firebase.auth().currentUser.getIdToken();
const r = await fetch(`${API}/api/gerar-link`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
  body: JSON.stringify({ nomeFicheiro: arquivo.name, tamanho: arquivo.size }),
});
const { urlUpload } = await r.json();

await enviarComProgresso(urlUpload, arquivo, 'video/mp4', p => console.log(Math.round(p * 100) + '%'));
```

A função `enviarComProgresso` manda o arquivo em pedaços:

```js
function enviarComProgresso(url, arquivo, contentType, aoProgredir) {
  const PARTE = 16 * 1024 * 1024;
  const esperar = (ms) => new Promise((r) => setTimeout(r, ms));
  const enviarParte = (inicio) => new Promise((resolve, reject) => {
    const fim = Math.min(inicio + PARTE, arquivo.size);
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `${url}&inicio=${inicio}`);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) aoProgredir((inicio + e.loaded) / arquivo.size); };
    xhr.onload = () => {
      let d = {};
      try { d = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status >= 200 && xhr.status < 300) return resolve(d);
      if (xhr.status === 409 && Number.isFinite(d.recebidos)) return resolve(d);
      if (xhr.status === 423 || xhr.status >= 500 || xhr.status === 0) return reject(Object.assign(new Error('rede'), { repetir: true }));
      reject(new Error(d.erro || `O servidor recusou o envio (${xhr.status}).`));
    };
    xhr.onerror = () => reject(Object.assign(new Error('rede'), { repetir: true }));
    xhr.send(arquivo.slice(inicio, fim));
  });
  return (async () => {
    let inicio = 0;
    let falhas = 0;
    while (inicio < arquivo.size) {
      try {
        const d = await enviarParte(inicio);
        if (d.concluido) break;
        inicio = Number(d.recebidos);
        falhas = 0;
      } catch (e) {
        if (!e.repetir) throw e;
        if (++falhas > 8) throw new Error('A ligação caiu durante o envio. Tenta de novo.');
        await esperar(Math.min(30000, 2000 * falhas));
      }
    }
    aoProgredir(1);
  })();
}
```

> A configuração web do Firebase (`apiKey`, `authDomain`...) **não é segredo**: ela identifica o projeto e é pública por natureza. O que protege o login são os **domínios autorizados** em *Firebase → Authentication → Settings*. Mesmo assim, se preferir, mantenha a página do site em outro repositório.

---

## Referência da API

### Rotas públicas

| Método | Rota | Autenticação | O que faz |
|---|---|---|---|
| `GET` | `/` | — | Teste de vida |
| `GET` | `/api/eu` | `Bearer <token Firebase>` | Quem está logado (`uid`, `nome`, `discordId`...) |
| `POST` | `/api/gerar-link` | `Bearer <token Firebase>` | Corpo `{ nomeFicheiro, tamanho }`. Responde `{ urlUpload, nomeUnico, contentType }` |
| `PUT` | `/api/upload/:chave?exp&b&sig&inicio=N` | assinatura no link | Recebe um pedaço do vídeo a partir do byte `N` |

Respostas do `PUT` em pedaços:

| Status | Corpo | Significado |
|---|---|---|
| `200` | `{ concluido: false, recebidos }` | Pedaço aceito; mande o próximo a partir de `recebidos` |
| `200` | `{ concluido: true }` | Vídeo completo e na fila |
| `409` | `{ recebidos }` | Posição errada; continue a partir de `recebidos` |
| `423` | `{ tentarDeNovo: true }` | O pedaço anterior ainda está chegando; tente em instantes |
| `403` | `{ erro }` | Link inválido ou expirado |
| `413` | `{ erro }` | Passou do tamanho declarado ou do limite |

Sem `inicio`, o `PUT` aceita o arquivo inteiro numa requisição só. Isso funciona localmente, mas proxies de hospedagem costumam recusar arquivos grandes.

### Rotas do worker

Todas exigem `Authorization: Bearer <WORKER_TOKEN>`.

| Método | Rota | O que faz |
|---|---|---|
| `GET` | `/worker/fila` | Lista os vídeos completos esperando análise |
| `POST` | `/worker/reivindicar` | `{ key }`. Move para `processando` (atômico) |
| `GET` | `/worker/meta?key=` | Ficha do jogador (nome, foto, ID do Discord) |
| `GET` | `/worker/video?key=` | Baixa o vídeo (aceita `Range` para retomar) |
| `DELETE` | `/worker/video?key=` | Apaga o vídeo |
| `DELETE` | `/worker/meta?key=` | Apaga a ficha (e a pasta, se ficar vazia) |
| `POST` | `/worker/falha` | `{ key, motivo }`. Marca como recusado |
| `POST` | `/worker/recuperar` | Devolve à fila vídeos presos há mais de 90 min. Depois de 3 tentativas, marca como falha |

---

## Onde ficam os vídeos

### Modo `site` (padrão)

```
videos/<ID do jogador>/<ID>_<timestamp>_<nome>.mp4/
    meta.json         ficha do jogador
    .parcial          envio em andamento (some sozinho após 6 h parado)
    pendente.mp4      esperando o worker
    processando.mp4   em análise
    falha.mp4         recusado (apagado após GUARDAR_FALHAS_DIAS)
    estado.json       tentativas, motivo, horário
```

- A pasta usa o **ID do Discord**, quando o login é pelo Discord, ou o `uid` do Firebase, sempre sem caracteres especiais.
- Envio que nunca chegou a ser feito (só o link foi gerado) é apagado depois de 24 h.

### Modo `r2`

O vídeo vai do navegador direto para o Cloudflare R2, por link pré-assinado, sem passar pela API. O worker usa os prefixos `processando/`, `falhas/` e `meta/` do bucket. Para usar, coloque `ARMAZENAMENTO=r2` e as variáveis `R2_*` nos dois `.env`.

---

## Como a análise funciona

1. **Calibração:**
   - **Amostragem:** pega cerca de 70 frames espalhados pelo vídeo.
   - **Escala:** testa escalas de 0,70x a 1,60x e refina em passos de 0,01.
   - **Papel e idioma:** decide sozinho, por votação de textos que só aparecem para um papel ou um idioma.
2. **Leitura:** a 2 frames por segundo, dividida entre vários processos. Ela procura imagens de referência na tela (*template matching* do OpenCV) e lê texto com o Tesseract.
3. **Separação de partidas:** usa o banner do mapa, a tela de resultado, os geradores voltando a 5 e a tela de fuga.
4. **Resultado oficial:** sai do emblema do fim da partida e do placar. Se não houver, usa o feed.

| Fonte na tela | Sobrevivente | Assassino |
|---|---|---|
| Feed de pontuação | PALLET DROP, KILLER STUN, VAULT, DASH DODGER, KILLER BLIND, PROTECTION, HOOK RESCUE, GREAT SKILL CHECK, REPAIRS, ESCAPED... | HIT, HOOKED (todo gancho), CAUGHT (1º gancho de cada um), DAMAGE GENERATOR, SACRIFICE, KILL, BLOODLUST e pop-ups de poder |
| Colchetes `( )` nos retratos | Tempo de chase (só os que têm ações suas) | — (a chase do assassino vem do feed) |
| Barra de ação | RECOVER, WIGGLE, REPAIR, HEAL, UNHOOK, OPEN ESCAPE | — |
| Status | Exhausted, Endurance, Haste, Broken | — |
| Contador de geradores | Gens feitos pelo time | Gens que faltam (pressão) |
| Losangos de perk | Confirma que a HUD está na tela | idem |
| Banner do mapa | Nome do mapa e início da partida | idem |
| Emblema + placar | Escapou / sacrificado / morto | Kills (Brutal, Impiedoso...), etiqueta **BOT** |

- **Resolução:** qualquer uma em 16:9.
- **Overlays:** funciona com overlay de teclado e de webcam no canto, desde que não cubram o feed nem os retratos.

**Assassinos com poder reconhecido:** Blight, Deathslinger, Lich, Canibal e Freddy. Nos outros, o worker conta hits, ganchos, chutes em gerador e chases normalmente, mas não mede a precisão do poder. Para ensinar um novo, recorte os textos do feed para `templates/assassino/<idioma>/score/`.

---

## Coach

Cada partida recebe uma **nota técnica (P, de 0 a 100)**, montada a partir de categorias:

| Sobrevivente | Peso base | Assassino | Peso |
|---|---|---|---|
| Chase | 25% | Letalidade | 25% |
| Objetivo | 25% | Pressão | 20% |
| Equipe | 15% | Chase | 20% |
| Recursos | 10% | Decisão | 15% |
| Decisão | 10% | Espalhamento | 10% |
| Sobrevivência | 10% | Poder | 10% |
| Mecânica | 5% | | |

- **Peso por função (sobrevivente):** quem passou a partida em chase não podia estar no gerador. Por isso, quanto mais tempo em chase, mais a Chase pesa e menos Objetivo/Equipe pesam (até 30 pontos percentuais).
- **Achados:** o coach cruza os dados e escreve observações. Exemplos:
  - **Sobrevivente:** "chase longa, mas sustentada por pallets", "pallet sem stun", "segurou o Killer enquanto o time fazia gen", "possível sacrifício estratégico".
  - **Assassino:** "ganchos concentrados em 2 sobreviventes", "o time fez 3 gens antes do seu 1º gancho", "poder pouco certeiro".

---

## Cálculo do MMR

O MMR é **separado por papel** (sobrevivente e assassino), no estilo Elo/Glicko:

```
Δ = K · (S − E) · U · C          limitado a ±DELTA_MAX por partida
```

| Termo | Significado |
|---|---|
| **E** | Expectativa contra o lobby médio: `1 / (1 + 10^((MMR_ref − MMR)/400))`. O `MMR_ref` é o MMR médio dos jogadores com 5 ou mais partidas |
| **S** | Resultado combinado. **Sobrevivente:** 30% fuga + 70% nota técnica (com sacrifício estratégico, a fuga pesa 10%). **Assassino:** 35% kills/4 + 65% nota técnica |
| **U** | Incerteza: de 1,5x numa conta nova (RD 350) a 0,5x numa conta estável (RD 50). O RD cai 5% por partida e volta a crescer com o tempo parado (~100 dias até o máximo) |
| **C** | Confiança: quanto da HUD foi lido (mínimo 0,3). Partida incompleta vale metade. Com bots: ×0,8 (sobrevivente) ou ×0,6 (assassino). Personalizada só com bots não conta |

---

## Banco de dados

Tabelas criadas e migradas sozinhas. O worker **só adiciona colunas**, nunca apaga dados.

**`jogadores`:** uma linha por jogador.

| Coluna | Descrição |
|---|---|
| `id_discord` | ID do jogador (ID do Discord ou `uid` do Firebase) |
| `nome_discord` | Nome exibido |
| `mmr`, `rd`, `partidas_jogadas`, `fugas`, `mmr_pico` | MMR de sobrevivente |
| `mmr_assassino`, `rd_assassino`, `partidas_assassino`, `kills_total`, `mmr_pico_assassino` | MMR de assassino |
| `ultima_atualizacao`, `ultima_assassino` | Última partida de cada papel |

**`historico_partidas`:** uma linha por partida analisada.

| Coluna | Descrição |
|---|---|
| `id_discord`, `papel`, `idioma`, `data_partida` | Quem, qual papel, idioma do jogo e quando |
| `mmr_anterior`, `mmr_novo`, `delta_mmr`, `rd_anterior`, `rd_novo` | Mudança de MMR |
| `performance_score`, `resultado_combinado`, `confianca_partida` | P, S e confiança |
| `resultado`, `resultado_tela`, `kills`, `bots`, `personalizada` | Resultado da partida |
| `mapa`, `duracao_s`, `partida_completa`, `escala_hud`, `perfil` | Contexto |
| `scores`, `metricas`, `achados`, `calculo_mmr` | Detalhes completos (JSONB) |
| `video_key`, `video_sha256`, `indice_partida`, `total_partidas_video` | Origem e anti-duplicata |
| `versao_motor` | Versão do worker que analisou |

Exemplo de ranking:

```sql
SELECT nome_discord, ROUND(mmr) AS mmr, partidas_jogadas
FROM jogadores
WHERE partidas_jogadas >= 10
ORDER BY mmr - 2 * rd DESC
LIMIT 20;
```

---

## Destaques (clipes)

Depois do relatório, o worker corta e posta até 3 clipes:

- **Sobrevivente:** as chases mais fortes (stuns, vaults, Dead Hard, cegadas, despistes), hit de proteção, resgate e fuga.
- **Assassino:** insta-chase (8 a 30 s terminando em queda), chase até a queda, queda com o poder e o último sacrifício de um 4K.

Cada clipe tem:
- até 40 s;
- 720p, H.264 com áudio;
- até 9,5 MB.

Cortar leva de 30 s a alguns minutos por partida, dependendo da CPU. O vídeo original só é apagado depois que os clipes são enviados.

---

## Comandos do worker

| Comando | O que faz |
|---|---|
| `python worker.py` | Fica rodando e olhando a fila |
| `python worker.py --uma-vez` | Processa o que estiver na fila e sai |
| `python worker.py --checar` | Confere `.env`, banco, fila, ffmpeg, Tesseract e idiomas do OCR |
| `python worker.py --teste-discord` | Manda um embed de teste no webhook |
| `python worker.py --local partida.mp4 --jogador <ID>` | Analisa um arquivo local e mostra o relatório, sem gravar nada |
| `... --gravar` | Junto com `--local`: grava o MMR e posta no Discord |
| `... --enviar` | Junto com `--local`: posta no Discord **sem** gravar no banco |
| `... --destaques PASTA` | Junto com `--local`: salva os clipes numa pasta |
| `... --json saida.json` | Junto com `--local`: salva eventos e resultado em JSON |

---

## Segurança

- **Nenhuma chave fica no código.** Tudo vem do `.env`, que está no `.gitignore`. Antes de cada commit, confira com `git status` se nenhum `.env` entrou.
- **Se um segredo vazar** (num commit, print ou chat), troque na hora:

  | Segredo | Onde trocar |
  |---|---|
  | `WORKER_TOKEN` | Gere outro e atualize os dois `.env` |
  | Webhook | *Editar canal → Integrações → Webhooks*: apague e crie outro |
  | Token do bot | *Developer Portal → Bot → Reset Token* |
  | Senha do banco | No painel do banco (Discloud ou Supabase) |
  | Chaves do R2 | *Cloudflare → R2 → Manage API tokens* |

- Um segredo que já foi commitado continua no histórico do Git, mesmo depois de apagado. **Troque o segredo**; não basta apagar o arquivo.
- **Identidade:** a API nunca confia no ID mandado pelo navegador; ele sempre sai do token do Firebase.
- **Links de envio:** assinados (HMAC), com validade, uso único e tamanho fixo. Mexer em qualquer parâmetro invalida a assinatura.
- **Rotas do worker:** protegidas por `WORKER_TOKEN`, comparado em tempo constante.
- **Caminhos de arquivo:** validados contra `../`. Nenhum nome vindo do usuário vira caminho direto.
- **`ALLOWED_ORIGINS`:** defina em produção, para que só o seu site possa chamar a API pelo navegador.

---

## Solução de problemas

| Sintoma | Causa provável | Solução |
|---|---|---|
| `Network is unreachable` / IPv6 ao conectar no banco | Endereço direto do Supabase (só IPv6) | Use a URL do **Session pooler** |
| `password authentication failed` | Senha errada ou com caracteres especiais | Troque por uma senha só com letras e números ou faça URL-encode |
| Navegador: *blocked by CORS policy* no `PUT` do vídeo | O proxy da hospedagem recusou um arquivo grande enviado de uma vez | Use o envio em pedaços (`&inicio=`) da função acima |
| Navegador: CORS no `gerar-link` | Origem fora do `ALLOWED_ORIGINS` | Adicione o domínio do site, com `https://` e sem barra no fim |
| Painel da Discloud: *Caminho inválido* na pasta `videos` | Pasta criada por versão antiga com `:` no nome | Reinicie o bot-mmr: a faxina apaga sozinha |
| Worker: `WORKER_TOKEN recusado pelo site` | Tokens diferentes nos dois `.env` | Use exatamente o mesmo valor |
| Relatório chega, clipes não | Clipes ainda sendo cortados, ou ffmpeg ausente | Espere a linha `Destaques: N de N` no log; confira `APT=ffmpeg` |
| Vídeo recusado por confiança baixa | HUD escondida, gravação cortada ou proporção diferente de 16:9 | Grave a tela inteira do jogo, com a HUD visível |
| `idiomas do OCR ... faltando: por` | Tesseract sem português | A pasta `tessdata/` precisa estar junto do `worker.py` |
| Worker caiu no meio de um vídeo | Reinício do app | Depois de ~90 min o vídeo volta à fila sozinho |

---

## Limitações e próximos passos

- **Grave até a tela de resultado e o placar** (uns 5 s depois do emblema). É dali que sai o resultado oficial.
- **Envio seletivo:** hoje o jogador escolhe quais partidas manda. Defesas planejadas:
  - ranking conservador (`MMR − 2·RD`) com mínimo de partidas;
  - placar público de enviadas/derrotas e alerta de desempenho fora da curva;
  - peso maior para vídeos com várias partidas seguidas e recusa de partidas antigas;
  - sessões com código combinado antes de jogar e selo "verificado" para partidas da comunidade.
- **Limiares do coach:** os limites de "alto/baixo" são valores iniciais. Com uma base maior de partidas, devem ser recalibrados pelos percentis reais.

---

Feito com 🩸 para a comunidade **Refúgio da Névoa**.