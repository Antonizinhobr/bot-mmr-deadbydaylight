require('dotenv').config();
const crypto = require('crypto');
const fs = require('fs');
const fsp = require('fs/promises');
const path = require('path');
const express = require('express');
const cors = require('cors');
const { Client, GatewayIntentBits } = require('discord.js');

const CFG = {
  porta: process.env.PORT || 8080,
  firebaseProjeto: process.env.FIREBASE_PROJECT_ID || 'dbd-camp',
  origens: (process.env.ALLOWED_ORIGINS || '').split(',').map(s => s.trim()).filter(Boolean),
  maxBytes: Number(process.env.MAX_TAMANHO_GB || 5) * 1024 ** 3,
  linksPorHora: Number(process.env.LINKS_POR_HORA || 6),
  validarTamanho: process.env.VALIDAR_TAMANHO !== '0',
  modoLegado: process.env.MODO_LEGADO === '1',
  bucket: process.env.R2_BUCKET_NAME,
  armazenamento: (process.env.ARMAZENAMENTO || (process.env.R2_BUCKET_NAME ? 'r2' : 'site')).toLowerCase(),
  pastaVideos: path.resolve(process.env.PASTA_VIDEOS || path.join(__dirname, 'videos')),
  urlPublica: (process.env.API_URL_PUBLICA || process.env.API_URL || 'https://botmmr.discloud.dev').replace(/\/+$/, ''),
  workerToken: process.env.WORKER_TOKEN || '',
  falhasDias: Number(process.env.GUARDAR_FALHAS_DIAS || 3),
};
const MODO_SITE = CFG.armazenamento === 'site';
const faltando = (MODO_SITE ? ['WORKER_TOKEN', 'DISCORD_TOKEN']
  : ['R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET_NAME', 'DISCORD_TOKEN'])
  .filter(n => !process.env[n]);
if (faltando.length) console.warn(`[CONFIG] Faltam no .env: ${faltando.join(', ')}`);
if (MODO_SITE && CFG.workerToken && CFG.workerToken.length < 32) {
  console.warn('[CONFIG] WORKER_TOKEN curto demais: use pelo menos 32 caracteres aleatórios.');
}

const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages] });

client.once('ready', () => console.log(`[DISCORD] Bot online como ${client.user.tag}`));

client.on('interactionCreate', async interaction => {
  if (!interaction.isChatInputCommand()) return;
  if (interaction.commandName === 'ping') {
    await interaction.reply('A Entidade está a observar. Pong!');
  }
});

const URL_CERTS = 'https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com';
let certs = { chaves: {}, expira: 0 };

async function chavesGoogle(forcar = false) {
  if (!forcar && Date.now() < certs.expira && Object.keys(certs.chaves).length) return certs.chaves;
  const r = await fetch(URL_CERTS);
  if (!r.ok) throw new Error(`certificados do Google indisponíveis (${r.status})`);
  const maxAge = Number(((r.headers.get('cache-control') || '').match(/max-age=(\d+)/) || [])[1] || 3600);
  const pem = await r.json();
  const chaves = {};
  for (const [kid, cert] of Object.entries(pem)) chaves[kid] = new crypto.X509Certificate(cert).publicKey;
  certs = { chaves, expira: Date.now() + maxAge * 1000 };
  return chaves;
}

const b64json = s => JSON.parse(Buffer.from(s, 'base64url').toString('utf8'));

async function verificarTokenFirebase(token) {
  if (typeof token !== 'string' || token.length > 4096) return null;
  const partes = token.split('.');
  if (partes.length !== 3) return null;
  let cab, dados;
  try { cab = b64json(partes[0]); dados = b64json(partes[1]); } catch { return null; }
  if (cab.alg !== 'RS256' || !cab.kid) return null;
  let chaves = await chavesGoogle();
  if (!chaves[cab.kid]) chaves = await chavesGoogle(true);
  const chave = chaves[cab.kid];
  if (!chave) return null;
  const ok = crypto.verify('RSA-SHA256', Buffer.from(`${partes[0]}.${partes[1]}`), chave,
    Buffer.from(partes[2], 'base64url'));
  if (!ok) return null;
  const agora = Math.floor(Date.now() / 1000);
  if (dados.aud !== CFG.firebaseProjeto) return null;
  if (dados.iss !== `https://securetoken.google.com/${CFG.firebaseProjeto}`) return null;
  if (!dados.exp || dados.exp < agora) return null;
  if (!dados.iat || dados.iat > agora + 300) return null;
  if (typeof dados.sub !== 'string' || !dados.sub || dados.sub.length > 128) return null;
  return {
    uid: dados.sub,
    nome: dados.name || dados.discord_nome || dados.username || (dados.email ? dados.email.split('@')[0] : null),
    email: dados.email || null,
    foto: dados.picture || dados.discord_avatar || null,
    provedor: (dados.firebase || {}).sign_in_provider || null,
    discordId: idDiscordDoToken(dados),
  };
}

const RE_SNOWFLAKE = /^\d{17,20}$/;
function idDiscordDoToken(dados) {
  const cands = [dados.discord_id, dados.discordId, dados.discord && dados.discord.id, dados.sub,
    String(dados.sub || '').replace(/^discord[:_-]/i, '')];
  for (const c of cands) if (c && RE_SNOWFLAKE.test(String(c))) return String(c);
  return null;
}

const RE_UID = /^[A-Za-z0-9]{6,64}$/;

async function exigirLogin(req, res, next) {
  const h = req.headers.authorization || '';
  const token = h.startsWith('Bearer ') ? h.slice(7).trim() : null;
  try {
    const u = token ? await verificarTokenFirebase(token) : null;
    if (u) {
      req.usuario = u;
      return next();
    }
  } catch (e) {
    console.error('[AUTH]', e.message);
    return res.status(503).json({ erro: 'Não consegui validar o login agora. Tente de novo em instantes.' });
  }
  const legado = String((req.body || {}).jogadorId || '');
  if (CFG.modoLegado && RE_UID.test(legado)) {
    console.warn(`[API] MODO_LEGADO: aceitando jogadorId do corpo (${legado}). Desligue depois de atualizar o site.`);
    req.usuario = { uid: legado, nome: null, email: null, foto: null, legado: true };
    return next();
  }
  return res.status(401).json({ erro: 'Sessão inválida ou expirada. Faça login novamente.' });
}

const envios = new Map();
function limitarTaxa(req, res, next) {
  const agora = Date.now();
  const lista = (envios.get(req.usuario.uid) || []).filter(t => agora - t < 3600 * 1000);
  if (lista.length >= CFG.linksPorHora) {
    return res.status(429).json({ erro: `Limite de ${CFG.linksPorHora} envios por hora. Tente mais tarde.` });
  }
  lista.push(agora);
  envios.set(req.usuario.uid, lista);
  next();
}

const EXTENSOES = ['.mp4', '.mov', '.mkv', '.webm'];
function limparNome(nome) {
  const arquivo = String(nome).split(/[\\/]/).pop();
  const ext = (arquivo.match(/\.[A-Za-z0-9]+$/) || [''])[0].toLowerCase();
  if (!EXTENSOES.includes(ext)) return null;
  const semExt = arquivo.slice(0, -ext.length).normalize('NFKD').replace(/[̀-ͯ]/g, '')
    .replace(/\s+/g, '_').replace(/[^A-Za-z0-9._-]/g, '').replace(/_+/g, '_').replace(/^[._-]+/, '').slice(0, 80);
  return `${semExt || 'partida'}${ext}`;
}

const RE_PASTA = /^[A-Za-z0-9_-]{1,128}$/;
const RE_ENVIO = /^([A-Za-z0-9_-]{1,128}?)_(\d{13})_([A-Za-z0-9._-]{1,90})$/;
const ESTADOS = ['pendente', 'processando', 'falha'];
const PARTE_MAX = 64 * 1024 * 1024;
const enviandoAgora = new Set();

function idDaPasta(usuario) {
  if (usuario.discordId && RE_SNOWFLAKE.test(usuario.discordId)) return usuario.discordId;
  const limpo = String(usuario.uid || '').replace(/[^A-Za-z0-9_-]/g, '_');
  return RE_PASTA.test(limpo) && /^[A-Za-z0-9].{4,}[A-Za-z0-9]$/.test(limpo) && !/_\d{13}_/.test(limpo) ? limpo
    : 'u' + crypto.createHash('sha256').update(String(usuario.uid)).digest('hex').slice(0, 27);
}

function pastaDoEnvio(chave) {
  const m = RE_ENVIO.exec(String(chave || ''));
  if (!m || m[3].includes('..')) return null;
  const pasta = path.resolve(CFG.pastaVideos, m[1], chave);
  return pasta.startsWith(CFG.pastaVideos + path.sep) ? pasta : null;
}

function extensaoDe(chave) {
  const ext = path.extname(chave).toLowerCase();
  return EXTENSOES.includes(ext) ? ext : '.mp4';
}

const arquivoVideo = (pasta, chave, estado) => path.join(pasta, estado + extensaoDe(chave));

function segredoUpload() {
  return crypto.createHmac('sha256', CFG.workerToken || 'sem-token').update('upload-de-video').digest();
}

function assinarUpload(chave, exp, bytes) {
  return crypto.createHmac('sha256', segredoUpload()).update(`${chave}.${exp}.${bytes}`).digest('base64url');
}

function iguais(a, b) {
  const ha = crypto.createHash('sha256').update(String(a)).digest();
  const hb = crypto.createHash('sha256').update(String(b)).digest();
  return crypto.timingSafeEqual(ha, hb);
}

async function lerJson(arquivo) {
  try { return JSON.parse(await fsp.readFile(arquivo, 'utf8')); } catch { return null; }
}

async function existe(arquivo) {
  try { return await fsp.stat(arquivo); } catch { return null; }
}

async function limparPastaVazia(pasta) {
  const itens = await fsp.readdir(pasta).catch(() => null);
  if (!itens) return;
  const sobra = itens.filter(n => n !== 'estado.json');
  if (sobra.length === 0) {
    await fsp.rm(pasta, { recursive: true, force: true });
    await fsp.rmdir(path.dirname(pasta)).catch(() => {});
  }
}

async function listarEnvios() {
  const lista = [];
  const jogadores = await fsp.readdir(CFG.pastaVideos, { withFileTypes: true }).catch(() => []);
  for (const j of jogadores) {
    if (!j.isDirectory()) continue;
    const envios = await fsp.readdir(path.join(CFG.pastaVideos, j.name), { withFileTypes: true }).catch(() => []);
    for (const e of envios) {
      if (!e.isDirectory() || !RE_ENVIO.test(e.name)) continue;
      const pasta = path.join(CFG.pastaVideos, j.name, e.name);
      const arquivos = await fsp.readdir(pasta).catch(() => []);
      lista.push({ chave: e.name, pasta, arquivos });
    }
  }
  return lista;
}

function exigirWorker(req, res, next) {
  const h = req.headers.authorization || '';
  const token = h.startsWith('Bearer ') ? h.slice(7).trim() : '';
  if (!MODO_SITE) return res.status(404).json({ erro: 'Armazenamento no site desligado.' });
  if (!CFG.workerToken || !token || !iguais(token, CFG.workerToken)) return res.status(401).json({ erro: 'Token do worker inválido.' });
  next();
}

function chaveDoPedido(req) {
  const bruta = String((req.body && req.body.key) || req.query.key || '');
  return bruta.startsWith('processando/') ? bruta.slice('processando/'.length) : bruta;
}

async function faxina() {
  if (!MODO_SITE) return;
  const agora = Date.now();
  for (const j of await fsp.readdir(CFG.pastaVideos, { withFileTypes: true }).catch(() => [])) {
    if (j.isDirectory() && !RE_PASTA.test(j.name)) {
      await fsp.rm(path.join(CFG.pastaVideos, j.name), { recursive: true, force: true }).catch(() => {});
      console.log(`[FAXINA] Pasta com nome inválido apagada: ${j.name}`);
    }
  }
  for (const { chave, pasta, arquivos } of await listarEnvios()) {
    try {
      const temVideo = arquivos.some(n => ESTADOS.some(e => n.startsWith(e + '.')));
      const parcial = await existe(path.join(pasta, '.parcial'));
      if (parcial && agora - parcial.mtimeMs > 6 * 3600e3) await fsp.rm(path.join(pasta, '.parcial'), { force: true });
      const falha = arquivos.find(n => n.startsWith('falha.'));
      if (falha) {
        const st = await existe(path.join(pasta, falha));
        if (st && agora - st.mtimeMs > CFG.falhasDias * 86400e3) {
          await fsp.rm(pasta, { recursive: true, force: true });
          await fsp.rmdir(path.dirname(pasta)).catch(() => {});
          console.log(`[FAXINA] Falha antiga apagada: ${chave}`);
          continue;
        }
      }
      if (!temVideo && !parcial) {
        const st = await existe(pasta);
        if (st && agora - st.mtimeMs > 24 * 3600e3) {
          await fsp.rm(pasta, { recursive: true, force: true });
          await fsp.rmdir(path.dirname(pasta)).catch(() => {});
        }
      }
    } catch (e) {
      console.error('[FAXINA]', chave, e.message);
    }
  }
}

const app = express();
app.set('trust proxy', 1);
app.use(cors({ origin: CFG.origens.length ? CFG.origens : true }));
app.use(express.json({ limit: '10kb' }));

let s3 = null;
let S3 = null;
if (!MODO_SITE) {
  S3 = { ...require('@aws-sdk/client-s3'), ...require('@aws-sdk/s3-request-presigner') };
  s3 = new S3.S3Client({
    region: 'auto',
    endpoint: `https://${process.env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com`,
    credentials: {
      accessKeyId: process.env.R2_ACCESS_KEY_ID,
      secretAccessKey: process.env.R2_SECRET_ACCESS_KEY,
    },
  });
}

app.get('/api/eu', exigirLogin, (req, res) => {
  const { uid, nome, email, foto, provedor, discordId } = req.usuario;
  res.json({ uid, nome, email, foto, provedor, discordId: discordId || null });
});

app.post('/api/gerar-link', exigirLogin, limitarTaxa, async (req, res) => {
  const { nomeFicheiro, tamanho } = req.body || {};
  if (!nomeFicheiro) return res.status(400).json({ erro: 'Faltam dados.' });
  const nomeLimpo = limparNome(nomeFicheiro);
  if (!nomeLimpo) return res.status(400).json({ erro: `Formato não suportado. Envie ${EXTENSOES.join(', ')}.` });
  const bytes = Number(tamanho);
  if (tamanho !== undefined && (!Number.isFinite(bytes) || bytes <= 0)) return res.status(400).json({ erro: 'Tamanho inválido.' });
  if (bytes > CFG.maxBytes) {
    return res.status(413).json({ erro: `Vídeo grande demais. Limite: ${CFG.maxBytes / 1024 ** 3} GB.` });
  }

  const ficha = {
    uid: req.usuario.uid, discord_id: req.usuario.discordId || null,
    nome: req.usuario.nome, email: req.usuario.email,
    foto: req.usuario.foto, provedor: req.usuario.provedor, arquivo_original: String(nomeFicheiro).slice(0, 200),
    tamanho: bytes || null, enviado_em: new Date().toISOString(), legado: !!req.usuario.legado,
  };

  if (MODO_SITE) {
    try {
      const idChave = idDaPasta(req.usuario);
      const nomeUnico = `${idChave}_${Date.now()}_${nomeLimpo}`;
      const pasta = pastaDoEnvio(nomeUnico);
      await fsp.mkdir(pasta, { recursive: true });
      await fsp.writeFile(path.join(pasta, 'meta.json'), JSON.stringify({ ...ficha, uid: idChave, uid_original: req.usuario.uid }));
      const exp = Math.floor(Date.now() / 1000) + 6 * 3600;
      const tam = bytes > 0 ? bytes : 0;
      const sig = assinarUpload(nomeUnico, exp, tam);
      const base = CFG.urlPublica || `${req.protocol}://${req.get('host')}`;
      const urlUpload = `${base}/api/upload/${encodeURIComponent(nomeUnico)}?exp=${exp}&b=${tam}&sig=${sig}`;
      console.log(`[API] Link gerado para: ${nomeUnico} (${req.usuario.nome || 'sem nome'})${req.usuario.legado ? ' [legado]' : ''}`);
      return res.json({ sucesso: true, urlUpload, nomeUnico, contentType: 'video/mp4' });
    } catch (erro) {
      console.error('[ERRO DISCO]:', erro);
      return res.status(500).json({ erro: 'Falha ao preparar o envio.' });
    }
  }

  const nomeUnico = `${req.usuario.uid}_${Date.now()}_${nomeLimpo}`;
  const assinarTamanho = CFG.validarTamanho && bytes > 0;
  const command = new S3.PutObjectCommand({
    Bucket: CFG.bucket,
    Key: nomeUnico,
    ContentType: 'video/mp4',
    ...(assinarTamanho ? { ContentLength: bytes } : {}),
  });

  try {
    await s3.send(new S3.PutObjectCommand({
      Bucket: CFG.bucket,
      Key: `meta/${nomeUnico}.json`,
      ContentType: 'application/json',
      Body: JSON.stringify(ficha),
    }));
    const urlUpload = await S3.getSignedUrl(s3, command, {
      expiresIn: 900,
      ...(assinarTamanho ? { signableHeaders: new Set(['content-type', 'content-length']) } : {}),
    });
    console.log(`[API] Link gerado para: ${nomeUnico} (${req.usuario.nome || 'sem nome'})${req.usuario.legado ? ' [legado]' : ''}`);
    res.json({ sucesso: true, urlUpload, nomeUnico, contentType: 'video/mp4' });
  } catch (erro) {
    console.error('[ERRO R2]:', erro);
    res.status(500).json({ erro: 'Falha ao gerar link.' });
  }
});

async function receberParte(req, res, chave, pasta, bytes) {
  const inicio = Number(req.query.inicio);
  const tamanho = Number(req.headers['content-length'] || -1);
  if (!(bytes > 0)) return res.status(400).json({ erro: 'Envio em partes precisa do tamanho do vídeo.' });
  if (!Number.isInteger(inicio) || inicio < 0) return res.status(400).json({ erro: 'Parte inválida.' });
  if (!(tamanho > 0) || tamanho > PARTE_MAX) return res.status(413).json({ erro: 'Parte grande demais.' });
  if (enviandoAgora.has(chave)) return res.status(423).json({ erro: 'Parte anterior ainda chegando.', tentarDeNovo: true });
  enviandoAgora.add(chave);
  const parcial = path.join(pasta, '.parcial');
  let atual = 0;
  let terminou = false;
  const soltar = () => enviandoAgora.delete(chave);
  try {
    const st = await existe(parcial);
    atual = st ? st.size : 0;
  } catch {
    atual = 0;
  }
  if (inicio !== atual) {
    soltar();
    req.resume();
    return res.status(409).json({ recebidos: atual });
  }
  if (inicio + tamanho > bytes) {
    soltar();
    res.once('finish', () => req.destroy());
    return res.status(413).json({ erro: 'O vídeo passou do tamanho informado.' });
  }
  const saida = fs.createWriteStream(parcial, { flags: 'a' });
  let recebidos = 0;
  const desistir = async (status, erro) => {
    if (terminou) return;
    terminou = true;
    req.unpipe(saida);
    saida.destroy();
    await fsp.truncate(parcial, atual).catch(() => {});
    soltar();
    if (!res.headersSent) {
      res.once('finish', () => req.destroy());
      res.status(status).json({ erro, recebidos: atual });
    }
  };
  req.on('data', pedaco => {
    recebidos += pedaco.length;
    if (recebidos > tamanho) desistir(400, 'Parte maior que o informado.');
  });
  req.on('aborted', () => desistir(400, 'Envio interrompido.'));
  req.on('close', () => { if (!req.complete) desistir(400, 'Envio interrompido.'); });
  saida.on('error', e => {
    console.error('[UPLOAD] Erro gravando no disco:', e.message);
    desistir(500, 'Falha ao gravar o vídeo no servidor.');
  });
  saida.on('finish', async () => {
    if (terminou) return;
    if (!req.complete || recebidos !== tamanho) return desistir(400, 'Parte incompleta.');
    terminou = true;
    const total = atual + recebidos;
    try {
      if (total === bytes) {
        await fsp.rename(parcial, arquivoVideo(pasta, chave, 'pendente'));
        console.log(`[UPLOAD] Recebido: ${chave} (${(total / 1e6).toFixed(0)} MB)`);
        res.json({ sucesso: true, concluido: true, recebidos: total, nomeUnico: chave });
      } else {
        res.json({ sucesso: true, concluido: false, recebidos: total });
      }
    } catch (e) {
      console.error('[UPLOAD]', e.message);
      res.status(500).json({ erro: 'Falha ao salvar o vídeo.', recebidos: atual });
    } finally {
      soltar();
    }
  });
  req.pipe(saida);
}

app.put('/api/upload/:chave', async (req, res) => {
  if (!MODO_SITE) return res.status(404).json({ erro: 'Armazenamento no site desligado.' });
  const chave = String(req.params.chave || '');
  const exp = Number(req.query.exp);
  const bytes = Number(req.query.b || 0);
  const sig = String(req.query.sig || '');
  const pasta = pastaDoEnvio(chave);
  if (!pasta || !Number.isFinite(exp) || !Number.isFinite(bytes) || bytes < 0) return res.status(400).json({ erro: 'Link inválido.' });
  if (exp < Date.now() / 1000) return res.status(403).json({ erro: 'Link expirado. Gere outro no site.' });
  if (!iguais(sig, assinarUpload(chave, exp, bytes))) return res.status(403).json({ erro: 'Link inválido.' });
  if (!(await existe(path.join(pasta, 'meta.json')))) return res.status(404).json({ erro: 'Envio não encontrado. Gere outro link.' });
  for (const e of ESTADOS) {
    if (await existe(arquivoVideo(pasta, chave, e))) {
      if (req.query.inicio !== undefined) {
        req.resume();
        return res.json({ sucesso: true, concluido: true, recebidos: bytes, nomeUnico: chave });
      }
      return res.status(409).json({ erro: 'Este vídeo já foi enviado.' });
    }
  }
  if (req.query.inicio !== undefined) return receberParte(req, res, chave, pasta, bytes);
  const declarado = Number(req.headers['content-length'] || 0);
  const limite = bytes > 0 ? bytes : CFG.maxBytes;
  if (declarado > limite) return res.status(413).json({ erro: `Vídeo grande demais. Limite: ${CFG.maxBytes / 1024 ** 3} GB.` });

  const parcial = path.join(pasta, '.parcial');
  let saida;
  try {
    saida = fs.createWriteStream(parcial, { flags: 'wx' });
    await new Promise((ok, falha) => { saida.once('open', ok); saida.once('error', falha); });
  } catch (e) {
    return res.status(409).json({ erro: 'Já existe um envio em andamento para este link.' });
  }

  let recebidos = 0;
  let terminou = false;
  const desistir = async (status, erro) => {
    if (terminou) return;
    terminou = true;
    req.unpipe(saida);
    saida.destroy();
    await fsp.rm(parcial, { force: true }).catch(() => {});
    if (!res.headersSent) {
      res.once('finish', () => req.destroy());
      res.status(status).json({ erro });
    }
  };

  req.on('data', pedaco => {
    recebidos += pedaco.length;
    if (recebidos > limite) desistir(413, `Vídeo grande demais. Limite: ${CFG.maxBytes / 1024 ** 3} GB.`);
  });
  req.on('aborted', () => desistir(400, 'Envio interrompido.'));
  req.on('close', () => { if (!req.complete) desistir(400, 'Envio interrompido.'); });
  req.on('error', () => desistir(400, 'Envio interrompido.'));
  saida.on('error', e => {
    console.error('[UPLOAD] Erro gravando no disco:', e.message);
    desistir(500, 'Falha ao gravar o vídeo no servidor.');
  });
  saida.on('finish', async () => {
    if (terminou) return;
    if (!req.complete || (bytes > 0 && recebidos !== bytes) || recebidos === 0) {
      return desistir(400, 'Envio incompleto. Tente de novo.');
    }
    terminou = true;
    try {
      await fsp.rename(parcial, arquivoVideo(pasta, chave, 'pendente'));
      console.log(`[UPLOAD] Recebido: ${chave} (${(recebidos / 1e6).toFixed(0)} MB)`);
      res.json({ sucesso: true, nomeUnico: chave });
    } catch (e) {
      console.error('[UPLOAD]', e.message);
      await fsp.rm(parcial, { force: true }).catch(() => {});
      res.status(500).json({ erro: 'Falha ao salvar o vídeo.' });
    }
  });
  req.pipe(saida);
});

app.get('/worker/fila', exigirWorker, async (req, res) => {
  const itens = [];
  for (const { chave, pasta, arquivos } of await listarEnvios()) {
    const nome = arquivos.find(n => n.startsWith('pendente.'));
    if (!nome) continue;
    const st = await existe(path.join(pasta, nome));
    if (st) itens.push({ key: chave, tamanho: st.size, modificado: st.mtimeMs });
  }
  itens.sort((a, b) => a.modificado - b.modificado);
  res.json({ itens });
});

app.post('/worker/reivindicar', exigirWorker, async (req, res) => {
  const chave = chaveDoPedido(req);
  const pasta = pastaDoEnvio(chave);
  if (!pasta) return res.status(400).json({ erro: 'Chave inválida.' });
  const destino = arquivoVideo(pasta, chave, 'processando');
  try {
    await fsp.rename(arquivoVideo(pasta, chave, 'pendente'), destino);
  } catch {
    return res.status(409).json({ erro: 'Outro worker já pegou ou o vídeo sumiu.' });
  }
  const estado = (await lerJson(path.join(pasta, 'estado.json'))) || {};
  estado.tentativas = Number(estado.tentativas || 0) + 1;
  estado.reivindicado_em = Date.now();
  await fsp.writeFile(path.join(pasta, 'estado.json'), JSON.stringify(estado));
  const st = await existe(destino);
  res.json({ key_proc: `processando/${chave}`, tentativas: estado.tentativas, tamanho: st ? st.size : 0 });
});

app.get('/worker/meta', exigirWorker, async (req, res) => {
  const pasta = pastaDoEnvio(chaveDoPedido(req));
  const meta = pasta && await lerJson(path.join(pasta, 'meta.json'));
  if (!meta) return res.status(404).json({ erro: 'Sem ficha.' });
  res.json(meta);
});

app.delete('/worker/meta', exigirWorker, async (req, res) => {
  const pasta = pastaDoEnvio(chaveDoPedido(req));
  if (!pasta) return res.status(400).json({ erro: 'Chave inválida.' });
  await fsp.rm(path.join(pasta, 'meta.json'), { force: true });
  await limparPastaVazia(pasta);
  res.json({ ok: true });
});

app.get('/worker/video', exigirWorker, async (req, res) => {
  const chave = chaveDoPedido(req);
  const pasta = pastaDoEnvio(chave);
  if (!pasta) return res.status(400).json({ erro: 'Chave inválida.' });
  const arquivo = arquivoVideo(pasta, chave, 'processando');
  const st = await existe(arquivo);
  if (!st) return res.status(404).json({ erro: 'Vídeo não está em processamento.' });
  let inicio = 0;
  const range = /^bytes=(\d+)-$/.exec(String(req.headers.range || ''));
  if (range) inicio = Math.min(Number(range[1]), st.size);
  res.statusCode = range ? 206 : 200;
  res.setHeader('Content-Type', 'application/octet-stream');
  res.setHeader('Accept-Ranges', 'bytes');
  res.setHeader('Content-Length', String(st.size - inicio));
  if (range) res.setHeader('Content-Range', `bytes ${inicio}-${st.size - 1}/${st.size}`);
  if (inicio >= st.size) return res.end();
  const leitura = fs.createReadStream(arquivo, { start: inicio });
  leitura.on('error', () => res.destroy());
  leitura.pipe(res);
});

app.delete('/worker/video', exigirWorker, async (req, res) => {
  const chave = chaveDoPedido(req);
  const pasta = pastaDoEnvio(chave);
  if (!pasta) return res.status(400).json({ erro: 'Chave inválida.' });
  for (const e of ESTADOS) await fsp.rm(arquivoVideo(pasta, chave, e), { force: true });
  await limparPastaVazia(pasta);
  console.log(`[WORKER] Vídeo apagado: ${chave}`);
  res.json({ ok: true });
});

app.post('/worker/falha', exigirWorker, async (req, res) => {
  const chave = chaveDoPedido(req);
  const pasta = pastaDoEnvio(chave);
  if (!pasta) return res.status(400).json({ erro: 'Chave inválida.' });
  const destino = arquivoVideo(pasta, chave, 'falha');
  try {
    await fsp.rename(arquivoVideo(pasta, chave, 'processando'), destino);
  } catch {
    await fsp.rename(arquivoVideo(pasta, chave, 'pendente'), destino).catch(() => {});
  }
  const agora = new Date();
  await fsp.utimes(destino, agora, agora).catch(() => {});
  const estado = (await lerJson(path.join(pasta, 'estado.json'))) || {};
  estado.motivo = String((req.body && req.body.motivo) || 'erro').slice(0, 500);
  estado.quando = agora.toISOString();
  await fsp.writeFile(path.join(pasta, 'estado.json'), JSON.stringify(estado)).catch(() => {});
  console.log(`[WORKER] Falha: ${chave} (${estado.motivo})`);
  res.json({ ok: true });
});

app.post('/worker/recuperar', exigirWorker, async (req, res) => {
  const idadeMin = Number((req.body || {}).idade_min || 90);
  const maxTent = Number((req.body || {}).max_tentativas || 3);
  const devolvidos = [];
  const falhas = [];
  for (const { chave, pasta, arquivos } of await listarEnvios()) {
    if (!arquivos.some(n => n.startsWith('processando.'))) continue;
    const estado = (await lerJson(path.join(pasta, 'estado.json'))) || {};
    if (Date.now() - Number(estado.reivindicado_em || 0) < idadeMin * 60e3) continue;
    const atual = arquivoVideo(pasta, chave, 'processando');
    if (Number(estado.tentativas || 1) >= maxTent) {
      await fsp.rename(atual, arquivoVideo(pasta, chave, 'falha')).catch(() => {});
      estado.motivo = 'travou o worker varias vezes';
      estado.quando = new Date().toISOString();
      await fsp.writeFile(path.join(pasta, 'estado.json'), JSON.stringify(estado)).catch(() => {});
      falhas.push(chave);
    } else {
      await fsp.rename(atual, arquivoVideo(pasta, chave, 'pendente')).catch(() => {});
      devolvidos.push(chave);
    }
  }
  res.json({ devolvidos, falhas });
});

app.get('/', (req, res) => {
  res.send('API do Refúgio da Névoa online.');
});

if (require.main === module) {
  const servidor = app.listen(CFG.porta, () => {
    console.log(`[API] Servidor online na porta ${CFG.porta}`);
    console.log(MODO_SITE ? `[API] Vídeos salvos no próprio site em ${CFG.pastaVideos}` : `[API] Vídeos vão para o R2 (${CFG.bucket})`);
  });
  servidor.requestTimeout = 0;
  servidor.timeout = 0;
  if (MODO_SITE) {
    fs.mkdirSync(CFG.pastaVideos, { recursive: true });
    faxina();
    setInterval(faxina, 3600e3).unref();
  }
  client.login(process.env.DISCORD_TOKEN);
}

module.exports = { app, verificarTokenFirebase, limparNome, CFG, faxina, pastaDoEnvio, assinarUpload, idDaPasta };