(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.DraftAutosave = api;
  }
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function createDraftSaveCoordinator(options) {
    let knownVersion = options.initialVersion;
    let latestGeneration = 0;
    let stopped = false;
    const maxStaleRetries = options.maxStaleRetries;

    function dispatch(generation, staleRetries) {
      if (stopped) {
        return Promise.resolve();
      }
      const payload = {
        answers: options.readAnswers(),
        expected_version: knownVersion,
      };
      let request;
      try {
        request = options.send(payload);
      } catch (error) {
        request = Promise.reject(error);
      }
      return Promise.resolve(request)
        .then(
          (result) => {
            if (stopped || generation !== latestGeneration) {
              return;
            }
            if (result.ok) {
              if (Number.isInteger(result.version) && result.version >= 0) {
                knownVersion = Math.max(knownVersion, result.version);
              }
              options.onSaved();
              return;
            }
            if (
              result.error &&
              result.error.code === "DRAFT_VERSION_STALE" &&
              Number.isInteger(result.current_version) &&
              result.current_version >= 0
            ) {
              knownVersion = Math.max(knownVersion, result.current_version);
              if (staleRetries >= maxStaleRetries) {
                options.onUnsaved();
                return;
              }
              return dispatch(generation, staleRetries + 1);
            }
            if (!stopped && generation === latestGeneration) {
              options.onUnsaved();
            }
          },
          () => {
            if (!stopped && generation === latestGeneration) {
              options.onUnsaved();
            }
          }
        );
    }

    function markChanged() {
      if (stopped) {
        return null;
      }
      latestGeneration += 1;
      options.onUnsaved();
      return latestGeneration;
    }

    return {
      markChanged,
      saveCurrent(generation) {
        if (stopped) {
          return Promise.resolve();
        }
        const selectedGeneration =
          generation === undefined ? markChanged() : generation;
        if (selectedGeneration !== latestGeneration) {
          return Promise.resolve();
        }
        return dispatch(selectedGeneration, 0);
      },
      stop() {
        stopped = true;
        latestGeneration += 1;
      },
    };
  }

  return { createDraftSaveCoordinator };
});
