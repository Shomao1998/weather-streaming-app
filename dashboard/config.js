// Where the dashboard reads its data from.
//
// Left empty, the page falls back to the sample documents in ./data, so the
// dashboard renders when opened straight from disk. The deploy workflow
// overwrites this file with the Function App hostname from the Bicep outputs.
window.WEATHER_CONFIG = {
  apiBase: "",
  refreshSeconds: 60,
};
