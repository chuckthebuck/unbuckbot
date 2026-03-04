// ==UserScript==
// @name         Commons Async Mass Rollback (Toolforge)
// @namespace    https://toolforge.org/
// @version      0.2.0
// @description  Adds a Commons portlet action to queue asynchronous rollback batches in Toolforge.
// @match        https://commons.wikimedia.org/*
// @grant        none
// ==/UserScript==

(function () {
  'use strict';

  const TOOL_ENDPOINT = 'https://YOUR-TOOL.toolforge.org';

  function addPortlet() {
    if (!window.mw || !mw.util) return;

    mw.util.addPortletLink(
      'p-cactions',
      '#',
      'Mass rollback',
      'ca-mass-rollback',
      'Queue async rollback tasks in Toolforge'
    );

    document.getElementById('ca-mass-rollback')?.addEventListener('click', async (event) => {
      event.preventDefault();
      await launchDialog();
    });
  }

  async function ensureAuth() {
    const authWindow = window.open(
      `${TOOL_ENDPOINT}/api/v1/auth/start`,
      'rollbackAuth',
      'width=680,height=820'
    );
    if (!authWindow) {
      alert('Popup blocked. Allow popups and retry.');
      return false;
    }

    await new Promise((resolve) => setTimeout(resolve, 3000));
    return true;
  }

  function parseInput(input) {
    return input
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const [title, user] = line.split('|').map((part) => part.trim());
        if (!title || !user) {
          throw new Error(`Invalid row: ${line}. Expected "Page title | Username".`);
        }
        return { title, user };
      });
  }

  async function launchDialog() {
    const raw = prompt(
      'Paste Commons rollback targets as "Page title | Username", one per line.\nExample:\nFile:Example.jpg | Vandal123'
    );
    if (!raw) return;

    let items;
    try {
      items = parseInput(raw);
    } catch (error) {
      alert(error.message);
      return;
    }

    if (!(await ensureAuth())) return;

    const response = await fetch(`${TOOL_ENDPOINT}/api/v1/jobs`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requested_by: mw.config.get('wgUserName') || '', items }),
    });

    if (!response.ok) {
      const text = await response.text();
      alert(`Submission failed (${response.status}): ${text}`);
      return;
    }

    const data = await response.json();
    alert(`Rollback job queued on Commons: ${data.job_id}`);
  }

  addPortlet();
})();
