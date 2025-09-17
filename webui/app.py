// ============================
// Based Maker — Frontend JS
// ============================
const API_BASE   = "https://web-production-626a.up.railway.app"; // tu Railway
const AUTH_TOKEN = "un_token_largo";                              // = WEBUI_AUTH_TOKEN

// SDKs
import * as hl from "https://esm.sh/@nktkas/hyperliquid@0.24.3";
import { createWalletClient, custom } from "https://esm.sh/viem@2.21.23";
import { privateKeyToAccount }       from "https://esm.sh/viem@2.21.23/accounts";

let nextIdx = 0;

function setText(id, txt){ const el=document.getElementById(id); if(el) el.textContent=txt; }
function logLine(s){
  const el = document.getElementById('logs');
  if(!el) return;
  el.textContent += (el.textContent ? "\n" : "") + s;
  el.scrollTop = el.scrollHeight;
}
function updateButtons(){
  const hasWallet = !!document.getElementById('walLocal').dataset.addr;
  const agentOk   = document.getElementById('agentState').dataset.ok === "1";
  document.getElementById('btnStart').disabled = !(hasWallet && agentOk);
  document.getElementById('btnStop').disabled  = !hasWallet;
}

// ---- agente en localStorage ----
function lsKey(addr){ return `agent:${addr.toLowerCase()}`; }
function getSavedAgent(addr){ try { return JSON.parse(localStorage.getItem(lsKey(addr)) || "null"); } catch { return null; } }
function saveAgent(addr, obj){ localStorage.setItem(lsKey(addr), JSON.stringify(obj)); }
function markAgent(ok, msg){
  const el = document.getElementById('agentState');
  el.dataset.ok = ok ? "1" : "0";
  el.innerHTML = ok ? `<span class="ok">Agente listo</span> · ${msg||''}` : `<span class="warn">Agente no listo</span> · ${msg||''}`;
  updateButtons();
}

// ---- API simple (sin preflight): /status y /logs GET, start/stop con ?token= y body text/plain ----
async function fetchStatus(){
  try{
    const r = await fetch(`${API_BASE}/status`, { method:'GET' });
    const j = await r.json();
    setText('running', j.running ? 'corriendo' : 'parado');
  }catch{ setText('running','offline'); }
}
async function pollLogs(){
  try{
    const r = await fetch(`${API_BASE}/logs?since=${nextIdx}`, { method:'GET' });
    const j = await r.json();
    nextIdx = j.next || 0;
    if (j.lines?.length){
      const el = document.getElementById('logs');
      el.textContent += "\n" + j.lines.join("\n");
      el.scrollTop = el.scrollHeight;
    }
  }catch{}
}

// ---- asegurar Mainnet para approveAgent ----
async function ensureMainnet(){
  const hex = await window.ethereum.request({ method: "eth_chainId" });
  if (hex !== "0x1"){
    await window.ethereum.request({ method: "wallet_switchEthereumChain", params: [{ chainId: "0x1" }] });
  }
}

// ---- crear + autorizar agente (1 firma) ----
async function ensureAgentFor(userAddr){
  const saved = getSavedAgent(userAddr);
  if (saved?.pk && saved?.agent && /^0x[0-9a-fA-F]{64}$/.test(saved.pk)){
    markAgent(true, `agent ${saved.agent.slice(0,6)}…${saved.agent.slice(-4)} (local)`);
    return saved;
  }

  logLine(`[AGENT] Generando agente local…`);
  const bytes = new Uint8Array(32); crypto.getRandomValues(bytes);
  const pk = "0x" + Array.from(bytes).map(b=>b.toString(16).padStart(2,'0')).join('');
  const acc = privateKeyToAccount(pk);
  const agentAddress = acc.address;

  logLine(`[AGENT] Autorizando agente ${agentAddress} en Hyperliquid…`);
  if (!window.ethereum) throw new Error("MetaMask no detectado");

  const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
  const primary  = accounts[0];
  await ensureMainnet();

  const walletClient = createWalletClient({ transport: custom(window.ethereum) });
  const walletForHL = {
    signMessage:   (args) => walletClient.signMessage({ ...args, account: primary }),
    signTypedData: (args) => walletClient.signTypedData({ ...args, account: primary }),
    getAddress: async () => primary,
  };
  const transport = new hl.HttpTransport({ url: "https://api.hyperliquid.xyz" });
  const exchange  = new hl.ExchangeClient({ wallet: walletForHL, transport });

  await exchange.approveAgent({ agentAddress }); // 1 firma
  logLine(`[AGENT] Aprobado (${agentAddress}).`);
  saveAgent(userAddr, { pk, agent: agentAddress, ts: Date.now() });
  markAgent(true, `agent ${agentAddress.slice(0,6)}…${agentAddress.slice(-4)}`);
  return { pk, agent: agentAddress };
}

// ---- Start/Stop SIN preflight (token por query + body text/plain) ----
async function startBot(){
  const addr  = document.getElementById('walLocal').dataset.addr;
  const agent = getSavedAgent(addr);
  if (!agent?.pk){ alert('Agente no listo. Reconectá la wallet.'); return; }

  const payload = {
    ticker: document.getElementById('ticker').value.trim(),
    amount_per_level: parseFloat(document.getElementById('amount').value),
    min_spread: parseFloat(document.getElementById('minspread').value),
    ttl: parseFloat(document.getElementById('ttl').value),
    maker_only: document.getElementById('maker').checked,
    testnet: false,
    agent_private_key: agent.pk
  };
  try{
    const r = await fetch(`${API_BASE}/start?token=${encodeURIComponent(AUTH_TOKEN)}`, {
      method:'POST',
      headers: { 'Content-Type':'text/plain' },   // simple request → sin preflight
      body: JSON.stringify(payload)
    });
    const txt = await r.text(); let j=null; try{ j=JSON.parse(txt); }catch{}
    if (!r.ok || !j?.ok){ logLine(`[HTTP ${r.status}] ${txt.slice(0,300)}`); alert('No se pudo iniciar'); return; }
    nextIdx = 0; document.getElementById('logs').textContent=''; await fetchStatus();
    logLine(`[WEB] Bot iniciado.`);
  }catch(e){ console.error(e); alert('Backend no disponible.'); }
}

async function stopBot(){
  try{
    const r = await fetch(`${API_BASE}/stop?token=${encodeURIComponent(AUTH_TOKEN)}`, {
      method:'POST',
      headers: { 'Content-Type':'text/plain' },
      body: ""
    });
    await fetchStatus();
    logLine(`[WEB] Bot detenido.`);
  }catch(e){ console.error(e); }
}

// ---- Conectar wallet ----
async function connectWallet(){
  if (!window.ethereum){ alert('Instalá MetaMask.'); return; }
  try{
    const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
    const addr = accounts[0];
    const el = document.getElementById('walLocal');
    el.textContent = `Wallet: ${addr}`; el.dataset.addr = addr;
    await ensureAgentFor(addr);
    updateButtons();
  }catch(e){
    console.error(e); logLine(`[ERROR] wallet/agent: ${e?.message||e}`); alert('No se pudo conectar/autorizar.');
  }
}

// ---- UI ----
document.getElementById('btnConnect').addEventListener('click', connectWallet);
document.getElementById('btnStart').addEventListener('click', startBot);
document.getElementById('btnStop').addEventListener('click', stopBot);

// ---- Init ----
fetchStatus(); updateButtons();
setInterval(() => { pollLogs(); fetchStatus(); }, 2000);
