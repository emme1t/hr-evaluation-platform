(() => {
  "use strict";

  const form = document.getElementById("evaluation-form");
  const status = document.getElementById("draft-status");
  if (!form || !status) {
    return;
  }

  const csrfInput = form.querySelector('input[name="csrfmiddlewaretoken"]');
  const submitButton = form.querySelector('button[type="submit"]');
  let timer = null;

  const currentAnswers = () => {
    const answers = {};
    form.querySelectorAll('input[type="radio"]:checked').forEach((input) => {
      answers[input.dataset.snapshotItemId] = Number(input.value);
    });
    return answers;
  };

  const markUnsaved = () => {
    status.textContent = "有未保存更改";
    status.dataset.state = "unsaved";
  };

  const coordinator = window.DraftAutosave.createDraftSaveCoordinator({
    initialVersion: Number(form.dataset.draftVersion),
    maxStaleRetries: 2,
    readAnswers: currentAnswers,
    send: async (payload) => {
      const response = await fetch(form.dataset.draftUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfInput.value,
        },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      return { ...result, ok: response.ok };
    },
    onSaved: () => {
      status.textContent = "已保存";
      status.dataset.state = "saved";
    },
    onUnsaved: markUnsaved,
  });

  form.addEventListener("change", (event) => {
    if (!event.target.matches('input[type="radio"]')) {
      return;
    }
    const generation = coordinator.markChanged();
    window.clearTimeout(timer);
    timer = window.setTimeout(() => coordinator.saveCurrent(generation), 800);
  });

  window.addEventListener("offline", markUnsaved);
  form.addEventListener("submit", () => {
    window.clearTimeout(timer);
    coordinator.stop();
    status.textContent = "正在提交…";
    if (submitButton) {
      submitButton.disabled = true;
    }
  });
})();
