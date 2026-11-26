"use strict";

const mobilePage = document.body.dataset.mobile === "true";
const authPanel = document.getElementById("mobile-auth");
const authStatus = document.getElementById("mobile-auth-status");

async function mobilePost(path, body = {}) {
  const response = await fetch("/api/mobile/" + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error("요청을 완료하지 못했습니다. 패스키 또는 등록 코드를 확인하세요.");
  return response.json();
}

const decode = (value) => Uint8Array.from(
  atob(value.replace(/-/g, "+").replace(/_/g, "/")), (character) => character.charCodeAt(0),
);
const encode = (value) => btoa(String.fromCharCode(...new Uint8Array(value)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function passkey(kind, code) {
  if (!window.isSecureContext || !window.PublicKeyCredential) {
    throw new Error("패스키를 지원하는 브라우저에서 3Q의 HTTPS 주소를 열어 주세요.");
  }
  const { options, request } = await mobilePost(kind + "/options", code ? { code } : {});
  options.challenge = decode(options.challenge);
  if (options.user) options.user.id = decode(options.user.id);
  for (const list of [options.allowCredentials, options.excludeCredentials]) {
    for (const entry of list || []) entry.id = decode(entry.id);
  }
  const credential = kind === "register"
    ? await navigator.credentials.create({ publicKey: options })
    : await navigator.credentials.get({ publicKey: options });
  if (!credential) throw new Error("패스키 요청을 다시 시도해 주세요.");
  const reply = credential.response;
  const fields = kind === "register"
    ? ["clientDataJSON", "attestationObject"]
    : ["clientDataJSON", "authenticatorData", "signature", "userHandle"];
  const response = Object.fromEntries(fields.map((field) => [field, reply[field] ? encode(reply[field]) : null]));
  await mobilePost(kind + "/verify", {
    request,
    credential: {
      id: credential.id,
      rawId: encode(credential.rawId),
      type: credential.type,
      response,
      clientExtensionResults: credential.getClientExtensionResults(),
    },
  });
}

function showMobileSession(signedIn) {
  if (!mobilePage) return;
  const changed = document.body.classList.toggle("mobile-authenticated", signedIn);
  const form = document.getElementById("personal-preferences");
  form.hidden = !signedIn;
  if (!changed && form.dataset.loaded) {
    form.reset();
    delete form.dataset.loaded;
    document.getElementById("personal-status").textContent = "";
  }
  if (signedIn && !form.dataset.loaded) loadPreferences(form);
  authPanel.hidden = signedIn;
  document.getElementById("mobile-logout").hidden = !signedIn;
}

async function loadPreferences(form) {
  form.dataset.loaded = "1";
  const status = document.getElementById("personal-status");
  try {
    const response = await fetch("/api/mobile/preferences");
    if (!response.ok) throw new Error();
    form.elements.vix_threshold.value = (await response.json()).vix_threshold ?? "";
  } catch {
    status.textContent = "개인 설정을 불러오지 못했습니다.";
  }
}

document.getElementById("personal-preferences").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const input = form.elements.vix_threshold;
  const status = document.getElementById("personal-status");
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    await mobilePost("preferences", {vix_threshold: input.value === "" ? null : input.valueAsNumber});
    status.textContent = "개인 설정을 저장했습니다.";
  } catch {
    status.textContent = "개인 설정을 저장하지 못했습니다.";
  } finally {
    button.disabled = false;
  }
});

if (mobilePage) {
  showMobileSession(false);
  async function authenticate(kind, code) {
    const buttons = authPanel.querySelectorAll("button");
    for (const button of buttons) button.disabled = true;
    authStatus.textContent = "패스키 확인 중…";
    try {
      await passkey(kind, code);
      document.getElementById("pairing-code").value = "";
      authStatus.textContent = "";
    } catch (error) {
      authStatus.textContent = error.name === "NotAllowedError"
        ? "패스키 확인이 취소됐거나 시간이 초과되었습니다." : error.message;
    } finally {
      for (const button of buttons) button.disabled = false;
    }
  }
  document.getElementById("passkey-login").addEventListener("click", () => authenticate("login"));
  document.getElementById("pairing-form").addEventListener("submit", (event) => {
    event.preventDefault();
    authenticate("register", document.getElementById("pairing-code").value);
  });
  document.getElementById("mobile-logout").addEventListener("click", async () => {
    try {
      await mobilePost("logout");
      showMobileSession(false);
    } catch {
      authStatus.textContent = "로그아웃에 실패했습니다. 연결을 확인해 주세요.";
    }
  });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js").catch(() => {});
}

const roleLabels = {owner: "소유자", viewer: "시세 보기"};

async function loadMobileDevices() {
  const list = document.getElementById("mobile-devices");
  try {
    const response = await fetch("/api/mobile/devices");
    if (!response.ok) throw new Error();
    const devices = await response.json();
    list.replaceChildren();
    if (!devices.length) {
      const empty = document.createElement("li");
      empty.textContent = "등록된 패스키가 없습니다.";
      list.append(empty);
    }
    for (const device of devices) {
      const item = document.createElement("li");
      const label = document.createElement("span");
      label.textContent = `${device.label || device.id.slice(0, 8)} · ${roleLabels[device.role]}`;
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.textContent = "접근 해제";
      revoke.addEventListener("click", async () => {
        if (!window.confirm(`${label.textContent} 패스키의 접근을 해제할까요?`)) return;
        try {
          await mobilePost("revoke", {id: device.id});
          await loadMobileDevices();
        } catch (error) {
          document.getElementById("admin-pairing-status").textContent = error.message;
        }
      });
      item.append(label, revoke);
      list.append(item);
    }
  } catch {
    const error = document.createElement("li");
    error.textContent = "패스키 목록을 불러오지 못했습니다.";
    list.replaceChildren(error);
  }
}

document.getElementById("refresh-passkeys").addEventListener("click", loadMobileDevices);

document.getElementById("issue-pairing").addEventListener("click", async () => {
  const status = document.getElementById("admin-pairing-status");
  try {
    const role = document.getElementById("invite-role").value;
    if (role === "owner" && !window.confirm("이 코드로 연결한 기기에서 계좌 조회와 한투 키 설정을 할 수 있습니다. 소유자 코드를 발급할까요?")) return;
    const result = await mobilePost("pair", {role, label: document.getElementById("invite-label").value});
    status.textContent = `${roleLabels[result.role]} · ${result.origin} · 등록 코드 ${result.code} · 5분 안에 사용하세요.`;
  } catch (error) {
    status.textContent = error.message;
  }
});

document.getElementById("reset-passkeys").addEventListener("click", async () => {
  if (!window.confirm("이 주소에서 등록한 모든 휴대폰 패스키를 삭제하고 로그인 세션을 종료할까요?")) return;
  const status = document.getElementById("admin-pairing-status");
  try {
    await mobilePost("reset");
    await loadMobileDevices();
    status.textContent = "등록한 패스키와 로그인 세션을 초기화했습니다.";
  } catch (error) {
    status.textContent = error.message;
  }
});
