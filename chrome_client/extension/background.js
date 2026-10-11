"use strict";

const HOST = "ru.privateline.chrome";
const LOCAL_PROXY = Object.freeze({
  mode: "fixed_servers",
  rules: { singleProxy: { scheme: "socks5", host: "127.0.0.1", port: 19590 },
           bypassList: ["<local>", "localhost", "127.0.0.1"] }
});

async function nativeMessage(payload) {
  try {
    const result = await chrome.runtime.sendNativeMessage(HOST, payload);
    if (!result || !result.ok) {
      return { ok: false, error: result?.error || "Локальный клиент не отвечает" };
    }
    return result;
  } catch (err) {
    return { ok: false, error: "Локальный клиент не установлен или недоступен. Проверьте установщик Chrome." };
  }
}

async function proxyEnabled() {
  const state = await chrome.proxy.settings.get({incognito: false});
  return state?.value?.mode === "fixed_servers" &&
    state?.value?.rules?.singleProxy?.scheme === "socks5" &&
    state?.value?.rules?.singleProxy?.host === "127.0.0.1" &&
    Number(state?.value?.rules?.singleProxy?.port) === 19590;
}

async function protectWebRTC(enabled) {
  try {
    if (enabled) {
      await chrome.privacy.network.webRTCIPHandlingPolicy.set({
        value:"disable_non_proxied_udp",scope:"regular"
      });
    } else {
      await chrome.privacy.network.webRTCIPHandlingPolicy.clear({scope:"regular"});
    }
  } catch (_) {
    // Managed browser policies may prevent changing this setting.
  }
}

async function sendStatus() {
  const host = await nativeMessage({ action: "status" });
  const proxy = await proxyEnabled();
  return {
    ok: true, installed: host.ok, running: host.ok && Boolean(host.running) && Boolean(host.verified),
    connected: host.ok && Boolean(host.running) && Boolean(host.verified) && proxy,
    proxy, hasProfile: host.ok && Boolean(host.has_profile),
    error: host.ok ? "" : host.error
  };
}

async function handle(request) {
  if (request?.action === "status") return sendStatus();

  if (request?.action === "connect") {
    if (typeof request.url !== "string" || !request.url.startsWith("vless://") || request.url.length > 8192) {
      return {ok:false,error:"Вставьте корректную ссылку VLESS Reality"};
    }
    const host = await nativeMessage({action:"connect", url:request.url});
    if (!host.ok) return host;
    try {
      await chrome.proxy.settings.set({value:LOCAL_PROXY,scope:"regular"});
      if (!(await proxyEnabled())) {
        return {ok:false,error:"Chrome не разрешил изменить прокси (проверьте другие расширения и политику браузера)"};
      }
      await protectWebRTC(true);
      await chrome.action.setBadgeText({text:"ON"});
      await chrome.action.setBadgeBackgroundColor({color:"#178b67"});
      return {ok:true, ip:host.ip || ""};
    } catch (_) {
      return {ok:false,error:"Chrome запретил установку прокси. Другие расширения VPN могут конфликтовать."};
    }
  }

  if (request?.action === "connect_saved") {
    const host = await nativeMessage({action:"connect_saved"});
    if (!host.ok) return host;
    try {
      await chrome.proxy.settings.set({value:LOCAL_PROXY,scope:"regular"});
      if (!(await proxyEnabled())) return {ok:false,error:"Другой компонент управляет прокси Chrome"};
      await protectWebRTC(true);
      await chrome.action.setBadgeText({text:"ON"});
      await chrome.action.setBadgeBackgroundColor({color:"#178b67"});
      return {ok:true, ip:host.ip || ""};
    } catch (_) {
      return {ok:false,error:"Не удалось включить SOCKS-прокси в Chrome"};
    }
  }

  if (request?.action === "disconnect") {
    // Disable browser routing first; if the native host is unavailable this
    // still leaves the user in an explicitly disconnected state.
    await chrome.proxy.settings.clear({scope:"regular"});
    await protectWebRTC(false);
    await chrome.action.setBadgeText({text:""});
    const host = await nativeMessage({action:"disconnect"});
    return {ok:true, warning:host.ok ? "" : "Прокси браузера отключён; локальный Xray проверить не удалось."};
  }
  return {ok:false,error:"Неизвестная команда"};
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  handle(request).then(sendResponse).catch(() => sendResponse({
    ok:false,error:"Ошибка управления подключением"
  }));
  return true;
});
chrome.runtime.onStartup.addListener(async () => {
  try {
    if (await proxyEnabled()) {
      const st = await nativeMessage({action:"status"});
      if (st.ok && st.running) {
        await protectWebRTC(true);
      await chrome.action.setBadgeText({text:"ON"});
        await chrome.action.setBadgeBackgroundColor({color:"#178b67"});
      } else {
        // Fail closed: don't silently downgrade traffic to DIRECT if the
        // native client died. User must explicitly disconnect/reconnect.
        await chrome.action.setBadgeText({text:"!"});
        await chrome.action.setBadgeBackgroundColor({color:"#b94643"});
      }
    }
  } catch (_) {}
});
