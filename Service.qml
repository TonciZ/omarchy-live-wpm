import QtQuick
import Quickshell
import Quickshell.Io

// Owns the single wpm-reader.py process (one per shell, not per bar) and
// exposes its once-a-second stats to the bar widget. All math and history
// live in the reader; this file only relays commands to its stdin.
Item {
  id: root

  readonly property string pluginId: "io.github.tonciz.live-wpm"
  property var shell: null
  property var manifest: null
  readonly property string pluginDir: manifest && manifest.__sourceDir
    ? String(manifest.__sourceDir)
    : String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "").replace(/\/$/, "")
  readonly property string readerPath: pluginDir === "" ? "" : pluginDir + "/scripts/wpm-reader.py"

  property var stats: null
  property string status: "starting"
  property string detail: ""
  property int restartAttempts: 0

  function send(obj) {
    if (reader.running) reader.write(JSON.stringify(obj) + "\n")
  }
  function setSetting(name, value) {
    var msg = { cmd: "settings" }
    msg[name] = value
    send(msg)
  }
  function resetDefaults() { send({ cmd: "defaults" }) }
  function resetSession() { send({ cmd: "reset" }) }
  function togglePause() { send({ cmd: "toggle-pause" }) }
  function ignoreCurrent() { send({ cmd: "ignore-current" }) }
  // Reader rebuilds report.html, answers {"type":"report"}, then we open it.
  function openReport() { send({ cmd: "report" }) }
  function ignoreRemove(pattern) { send({ cmd: "ignore-remove", pattern: pattern }) }

  Process {
    id: reader
    stdinEnabled: true
    command: ["/usr/bin/python3", "-I", root.readerPath]
    running: root.readerPath !== ""
    stdout: SplitParser {
      onRead: function(line) {
        try {
          var msg = JSON.parse(line)
          if (msg.type === "stats") {
            root.stats = msg
            root.status = String(msg.status || "error")
          } else if (msg.type === "report" && msg.path) {
            opener.command = ["xdg-open", String(msg.path)]
            opener.running = true
          }
        } catch (e) {}
      }
    }
    stderr: SplitParser {
      onRead: function(line) {
        var d = String(line || "").trim()
        if (d !== "") root.detail = d.slice(0, 256)
      }
    }
    onExited: function(exitCode) {
      root.status = "error"
      restartTimer.interval = Math.min(30000, 1000 * Math.pow(2, root.restartAttempts))
      root.restartAttempts += 1
      restartTimer.restart()
    }
  }

  Process { id: opener }

  Timer {
    id: restartTimer
    repeat: false
    onTriggered: reader.running = true
  }

  // Forget earlier failures once the reader has stayed up for a minute.
  Timer {
    interval: 60000
    repeat: true
    running: reader.running
    onTriggered: root.restartAttempts = 0
  }

  IpcHandler {
    target: "io.github.tonciz.live-wpm"
    function togglePause(): void { root.togglePause() }
    function resetSession(): void { root.resetSession() }
    function openReport(): void { root.openReport() }
    function state(): string { return JSON.stringify(root.stats) }
  }
}
