const assert = require("node:assert/strict");
const test = require("node:test");

const {
  createDraftSaveCoordinator,
} = require("../../../../static/js/draft-state.js");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, reject, resolve };
}

function harness(maxStaleRetries = 2) {
  let domScore = 2;
  let state = "unsaved";
  let savedCalls = 0;
  let unsavedCalls = 0;
  const database = { score: null, version: 0 };
  const requests = [];
  const coordinator = createDraftSaveCoordinator({
    initialVersion: 0,
    maxStaleRetries,
    readAnswers: () => ({ item: domScore }),
    send: (payload) => {
      const completion = deferred();
      requests.push({ completion, payload });
      return completion.promise;
    },
    onSaved: () => {
      state = "saved";
      savedCalls += 1;
    },
    onUnsaved: () => {
      state = "unsaved";
      unsavedCalls += 1;
    },
  });

  function complete(index) {
    const request = requests[index];
    if (request.payload.expected_version !== database.version) {
      request.completion.resolve({
        current_version: database.version,
        error: { code: "DRAFT_VERSION_STALE" },
        ok: false,
      });
      return;
    }
    database.score = request.payload.answers.item;
    database.version += 1;
    request.completion.resolve({ ok: true, version: database.version });
  }

  function fail(index) {
    requests[index].completion.reject(new Error("network failure"));
  }

  return {
    complete,
    coordinator,
    database,
    fail,
    requests,
    savedCalls: () => savedCalls,
    setDomScore(value) {
      domScore = value;
    },
    state: () => state,
    unsavedCalls: () => unsavedCalls,
  };
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
}

test("newer request completing first prevents an old retry or saved marker", async () => {
  const app = harness();
  const older = app.coordinator.saveCurrent();
  app.setDomScore(5);
  const current = app.coordinator.saveCurrent();

  app.complete(1);
  await flush();
  app.complete(0);
  await Promise.all([older, current]);

  assert.equal(app.database.score, 5);
  assert.equal(app.database.version, 1);
  assert.equal(app.requests.length, 2);
  assert.equal(app.state(), "saved");
});

test("older request completing first retries current DOM with current version", async () => {
  const app = harness();
  const older = app.coordinator.saveCurrent();
  app.setDomScore(5);
  const current = app.coordinator.saveCurrent();

  app.complete(0);
  await flush();
  assert.equal(app.state(), "unsaved");
  app.complete(1);
  await flush();

  assert.equal(app.requests.length, 3);
  assert.deepEqual(app.requests[2].payload, {
    answers: { item: 5 },
    expected_version: 1,
  });
  app.complete(2);
  await Promise.all([older, current]);

  assert.equal(app.database.score, 5);
  assert.equal(app.database.version, 2);
  assert.equal(app.state(), "saved");
});

test("stale retries are bounded and leave the latest state unsaved", async () => {
  const app = harness(2);
  const saving = app.coordinator.saveCurrent();

  for (let index = 0; index < 3; index += 1) {
    app.database.version += 1;
    app.complete(index);
    await flush();
  }
  await saving;

  assert.equal(app.requests.length, 3);
  assert.equal(app.state(), "unsaved");
});

test("input during debounce invalidates an older success and its version", async () => {
  const app = harness();
  const older = app.coordinator.saveCurrent();

  app.setDomScore(5);
  const currentGeneration = app.coordinator.markChanged();
  app.complete(0);
  await older;

  assert.equal(app.state(), "unsaved");
  assert.equal(app.savedCalls(), 0);
  assert.equal(app.requests.length, 1);

  const current = app.coordinator.saveCurrent(currentGeneration);
  assert.deepEqual(app.requests[1].payload, {
    answers: { item: 5 },
    expected_version: 0,
  });
  app.complete(1);
  await flush();
  assert.deepEqual(app.requests[2].payload, {
    answers: { item: 5 },
    expected_version: 1,
  });
  app.complete(2);
  await current;

  assert.equal(app.database.score, 5);
  assert.equal(app.state(), "saved");
  assert.equal(app.savedCalls(), 1);
});

test("new input supersedes an in-flight stale retry and its success", async () => {
  const app = harness();
  app.database.version = 1;
  const generationB = app.coordinator.markChanged();
  const savingB = app.coordinator.saveCurrent(generationB);

  app.complete(0);
  await flush();
  assert.deepEqual(app.requests[1].payload, {
    answers: { item: 2 },
    expected_version: 1,
  });

  app.setDomScore(4);
  const generationC = app.coordinator.markChanged();
  app.complete(1);
  await savingB;

  assert.equal(app.state(), "unsaved");
  assert.equal(app.savedCalls(), 0);

  const savingC = app.coordinator.saveCurrent(generationC);
  assert.deepEqual(app.requests[2].payload, {
    answers: { item: 4 },
    expected_version: 1,
  });
  app.complete(2);
  await flush();
  assert.deepEqual(app.requests[3].payload, {
    answers: { item: 4 },
    expected_version: 2,
  });
  app.complete(3);
  await savingC;

  assert.equal(app.database.score, 4);
  assert.equal(app.state(), "saved");
  assert.equal(app.savedCalls(), 1);
});

test("only a current generation network error applies an unsaved effect", async () => {
  const app = harness();
  const older = app.coordinator.saveCurrent();
  app.setDomScore(5);
  const currentGeneration = app.coordinator.markChanged();
  const unsavedAfterInput = app.unsavedCalls();

  app.fail(0);
  await older;
  assert.equal(app.state(), "unsaved");
  assert.equal(app.unsavedCalls(), unsavedAfterInput);
  assert.equal(app.savedCalls(), 0);

  const current = app.coordinator.saveCurrent(currentGeneration);
  app.fail(1);
  await current;
  assert.equal(app.state(), "unsaved");
  assert.equal(app.unsavedCalls(), unsavedAfterInput + 1);
  assert.equal(app.savedCalls(), 0);
});
