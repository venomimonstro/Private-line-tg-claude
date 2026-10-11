"use strict";
const $ = id => document.getElementById(id);
const buttons = ["connect","saved","disconnect"];
async function call(action, url) {
  const request = url ? {action,url} : {action};
  return chrome.runtime.sendMessage(request);
}
function busy(flag) { for (const id of buttons) $(id).disabled = flag; }
function render(s) {
  $("status-dot").className = "dot" + (s.connected ? " on" : (s.proxy && !s.running ? " error" : ""));
  $("status-title").textContent = s.connected ? "VPN подключён" : "VPN отключён";
  $("status-detail").textContent = s.connected ? "SOCKS5 · локальный Xray" :
    s.proxy && !s.running ? "Прокси включён, Xray недоступен" :
    s.installed ? "Готов к подключению" : "Нужна установка локального клиента";
  $("saved").hidden = !s.hasProfile;
  if (s.error) $("message").textContent = s.error;
}
async function refresh() {
  const s = await call("status");
  render(s);
}
async function execute(action) {
  busy(true);
  $("message").textContent = "";
  try {
    const url = action === "connect" ? $("vless").value.trim() : undefined;
    const result = await call(action, url);
    if (!result?.ok) $("message").textContent = result?.error || "Ошибка соединения";
    else if (action === "connect" || action === "connect_saved") {
      $("vless").value = ""; // credentials not retained by the extension
      $("message").textContent = result.ip ? "Проверен внешний IP: " + result.ip : "Туннель запущен";
    } else if (result.warning) $("message").textContent = result.warning;
    await refresh();
  } catch (_) {
    $("message").textContent = "Ошибка связи с расширением";
  } finally { busy(false); }
}
$("connect").addEventListener("click",()=>execute("connect"));
$("saved").addEventListener("click",()=>execute("connect_saved"));
$("disconnect").addEventListener("click",()=>execute("disconnect"));
refresh().catch(()=>{$("message").textContent="Не удалось проверить состояние VPN";});
