const DEFAULT_PORT = 9333;
const portInput = document.getElementById("port");
const status = document.getElementById("status");

chrome.storage.local.get(["wcPort"]).then(({ wcPort }) => {
  portInput.value = wcPort || DEFAULT_PORT;
});

document.getElementById("save").addEventListener("click", async () => {
  const port = parseInt(portInput.value, 10);
  if (!port || port < 1 || port > 65535) {
    status.textContent = "invalid port";
    status.className = "";
    return;
  }
  await chrome.storage.local.set({ wcPort: port });
  status.textContent = "saved";
  status.className = "saved";
});
