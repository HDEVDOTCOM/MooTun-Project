"use strict";

(() => {
  const panels = {
    summary: document.getElementById("summary-data"),
    transactions: document.getElementById("transactions-data"),
    savings: document.getElementById("savings-data"),
  };
  const status = document.getElementById("status");
  const login = document.getElementById("login");
  let generation = 0;
  let controller = null;
  let initialization = null;
  let initialized = false;

  function textElement(tag, text, className) {
    const element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function clearPanels(message) {
    Object.values(panels).forEach((panel) => panel.replaceChildren(textElement("p", message)));
  }

  function invalidate(message) {
    generation += 1;
    if (controller) controller.abort();
    controller = null;
    clearPanels(message);
  }

  // Group exact backend decimal strings; no floating-point money calculation.
  function baht(value) {
    const [integer, fraction] = value.split(".");
    return `${integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${fraction} บาท`;
  }

  function calendarDate(value) {
    const [year, month, day] = value.split("-");
    return `${day}/${month}/${Number(year) + 543}`;
  }

  function freshness(panel, value) {
    const timestamp = new Date(value).toLocaleString("th-TH", { timeZone: "Asia/Bangkok" });
    panel.append(textElement("p", `ข้อมูล ณ ${timestamp} (เวลาไทย)`, "freshness"));
  }

  function pairs(panel, items) {
    const list = document.createElement("dl");
    items.forEach(([label, value]) => list.append(textElement("dt", label), textElement("dd", value)));
    panel.append(list);
  }

  function renderSummary(data) {
    const panel = panels.summary;
    panel.replaceChildren(textElement("p", `เดือน ${data.month}/${data.year_be}`));
    pairs(panel, [
      ["รายรับ", baht(data.income_baht)], ["รายจ่าย", baht(data.expense_baht)],
      ["สุทธิเดือนนี้", baht(data.balance_baht)], ["จำนวนรายการ", String(data.transaction_count)],
    ]);
    if (data.transaction_count === 0) panel.append(textElement("p", "ยังไม่มีรายการในเดือนนี้"));
    freshness(panel, data.as_of);
  }

  function renderTransactions(data) {
    const panel = panels.transactions;
    panel.replaceChildren();
    if (data.items.length === 0) panel.append(textElement("p", "ยังไม่มีรายการ"));
    const list = document.createElement("ul");
    data.items.forEach((item) => {
      const direction = item.transaction_type === "income" ? "รายรับ +" : "รายจ่าย −";
      const row = textElement("li", `${calendarDate(item.occurred_on)} · ${direction}${baht(item.amount_baht)} · ${item.category}`);
      if (item.description) row.append(textElement("p", item.description));
      list.append(row);
    });
    panel.append(list);
    freshness(panel, data.as_of);
  }

  function renderSavings(data) {
    const panel = panels.savings;
    const goal = data.goal;
    panel.replaceChildren();
    if (!goal) {
      panel.append(textElement("p", "ยังไม่มีเป้าหมายการออม ลองพิมพ์ “ตั้งเป้า 1500 ซื้อหนังสือ” ในแชต"));
    } else {
      panel.append(textElement("h3", goal.title));
      pairs(panel, [["เป้าหมาย", baht(goal.target_baht)], ["ออมแล้ว", baht(goal.saved_baht)], ["เหลือ", baht(goal.remaining_baht)]]);
      const progress = document.createElement("progress");
      progress.max = 100;
      progress.value = goal.progress_percent;
      progress.setAttribute("aria-label", "ความคืบหน้าการออม");
      panel.append(progress, textElement("p", `ความคืบหน้า ${goal.progress_percent}%`));
      if (goal.deadline) panel.append(textElement("p", `กำหนด ${calendarDate(goal.deadline)}`));
    }
    freshness(panel, data.as_of);
  }

  async function initialize() {
    if (!initialization) {
      initialization = (async () => {
        const response = await fetch("/app/config.json", { cache: "no-store", credentials: "omit" });
        if (!response.ok) throw new Error("WEBAPP_UNAVAILABLE");
        const config = await response.json();
        if (!window.liff) throw new Error("AUTH_UNAVAILABLE");
        // Only openid is required. Scopes are configured in LINE, not browser-selected.
        // Leave SDK query parameters intact until its initialization has completed.
        await liff.init({ liffId: config.liff_id, withLoginOnExternalBrowser: false });
        initialized = true;
      })().catch((error) => {
        initialization = null;
        throw error;
      });
    }
    await initialization;
  }

  function authRequired() {
    invalidate("กรุณาเข้าสู่ระบบ LINE เพื่อดูข้อมูล");
    status.textContent = "ต้องยืนยันตัวตน LINE อีกครั้ง หากยกเลิกการอนุญาต สามารถลองเข้าสู่ระบบใหม่ได้";
    login.hidden = false;
  }

  async function financial(path, token, signal) {
    const response = await fetch(path, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store", credentials: "omit", signal,
    });
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.code || "DATA_UNAVAILABLE");
      error.status = response.status;
      throw error;
    }
    return data;
  }

  async function load() {
    invalidate("กำลังโหลดข้อมูล…");
    const current = generation;
    const active = new AbortController();
    controller = active;
    login.hidden = true;
    status.textContent = "กำลังเชื่อมต่อ LINE…";
    const isCurrent = () => current === generation && !active.signal.aborted;

    function failure(panel, error) {
      if (!isCurrent()) return;
      if (error.status === 401) {
        authRequired();
      } else if (error.message === "AUTH_UNAVAILABLE") {
        invalidate("การยืนยัน LINE ไม่พร้อมใช้งานชั่วคราว กรุณาลองอีกครั้ง");
        status.textContent = "การยืนยัน LINE ไม่พร้อมใช้งานชั่วคราว กรุณาลองอีกครั้ง";
      } else {
        panel.replaceChildren(textElement("p", "ข้อมูลไม่พร้อมใช้งานชั่วคราว กรุณาลองอีกครั้ง"));
        status.textContent = "โหลดข้อมูลบางส่วนไม่สำเร็จ กรุณาลองอีกครั้ง";
      }
    }

    try {
      await initialize();
      if (!isCurrent()) return;
      const token = liff.getIDToken();
      if (!token) {
        authRequired();
        return;
      }
      status.textContent = "กำลังโหลดข้อมูล…";
      // Summary is the identity bootstrap. No other financial read precedes it.
      let summaryError = null;
      try {
        const data = await financial("/api/me/summary", token, active.signal);
        if (isCurrent()) renderSummary(data);
      } catch (error) {
        summaryError = error;
        failure(panels.summary, error);
      }
      if (!isCurrent()) return;
      if (summaryError && summaryError.message !== "DATA_UNAVAILABLE") {
        clearPanels("ข้อมูลไม่พร้อมใช้งานชั่วคราว กรุณาลองอีกครั้ง");
        return;
      }
      let partial = Boolean(summaryError);
      await Promise.all([
        ["/api/me/transactions/recent", panels.transactions, renderTransactions],
        ["/api/me/savings", panels.savings, renderSavings],
      ].map(async ([path, panel, render]) => {
        try {
          const data = await financial(path, token, active.signal);
          if (isCurrent()) render(data);
        } catch (error) {
          partial = true;
          failure(panel, error);
        }
      }));
      if (isCurrent()) status.textContent = partial ? "โหลดข้อมูลบางส่วนไม่สำเร็จ กรุณาลองอีกครั้ง" : "โหลดข้อมูลแล้ว";
    } catch (error) {
      if (!isCurrent()) return;
      invalidate("ยังไม่สามารถเชื่อมต่อได้ กรุณาลองอีกครั้ง หรือใช้งานผ่านแชต");
      status.textContent = error.message === "WEBAPP_UNAVAILABLE"
        ? "หน้าเว็บไม่พร้อมใช้งานชั่วคราว ยังใช้งานผ่านแชตได้"
        : "เชื่อมต่อ LINE ไม่สำเร็จ หากยกเลิกการอนุญาต สามารถลองอีกครั้งได้";
      login.hidden = !initialized;
    }
  }

  login.addEventListener("click", () => {
    invalidate("กรุณาเข้าสู่ระบบ LINE เพื่อดูข้อมูล");
    try {
      // Explicit recovery, even if isLoggedIn is true but the ID token is absent.
      // No automatic login loop. Fixed same-origin return path, without credentials.
      if (liff.isLoggedIn()) liff.logout();
      liff.login({ redirectUri: new URL("/app/", window.location.origin).href });
    } catch (_) {
      status.textContent = "เข้าสู่ระบบ LINE ไม่สำเร็จ กรุณาลองอีกครั้ง";
    }
  });
  document.getElementById("refresh").addEventListener("click", load);
  window.addEventListener("pagehide", () => invalidate("กลับมาที่หน้านี้เพื่อโหลดข้อมูลใหม่"));
  window.addEventListener("pageshow", () => { if (!document.hidden) load(); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) invalidate("กลับมาที่หน้านี้เพื่อโหลดข้อมูลใหม่");
    else load();
  });
  const view = new URLSearchParams(window.location.search).get("view");
  if (["summary", "transactions", "savings", "help"].includes(view)) {
    document.getElementById(view).scrollIntoView();
  }
})();
