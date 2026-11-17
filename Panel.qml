import QtQuick
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Bar label ("72 wpm") + popup with live/session numbers, history and three
// sliders. State comes from Service.qml (wpm-reader.py).
Panel {
  id: root
  moduleName: "io.github.tonciz.live-wpm"
  ipcTarget: "io.github.tonciz.live-wpm.panel"

  readonly property var svc: bar && bar.shell && typeof bar.shell.serviceFor === "function"
    ? bar.shell.serviceFor("io.github.tonciz.live-wpm") : null
  readonly property var st: svc ? svc.stats : null
  readonly property var cfg: st ? st.settings : null
  readonly property var ranges: st ? st.ranges : null
  readonly property bool paused: st ? st.paused === true : false
  readonly property bool ignored: st ? st.ignored === true : false
  readonly property bool idle: st ? st.idle === true : true
  readonly property var liveWpm: st ? st.live : null
  readonly property color fg: root.bar ? root.bar.foreground : "white"
  readonly property bool typing: !root.idle && !root.paused && !root.ignored
  // The shell has no green; read the theme's own from colors.toml.
  property color green: "#2d9a48"
  FileView {
    path: Commons.Color.currentThemePath + "/colors.toml"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      var match = text().match(/^\s*(?:green|color2)\s*=\s*["']?(#[0-9A-Fa-f]{6})/m)
      if (match) root.green = match[1]
    }
  }
  readonly property color dim: Qt.darker(fg, 1.4)
  readonly property string fontFamily: root.bar ? root.bar.fontFamily : Style.font.family

  function n(v) { return v === null || v === undefined ? "–" : String(Math.round(v)) }
  function fmt(key, v) {
    if (key === "pause_cutoff_ms" || key === "end_hesitation_ms") return (v / 1000).toFixed(2) + " s"
    if (key === "end_trim_keys") return v === 0 ? "off" : v + (v === 1 ? " key" : " keys")
    if (key === "min_burst_keys") return v + " keys"
    if (key === "peak_min_chars") return v + " chars"
    return v + " s"
  }
  // "com\\.nvidia\\.geforcenow" -> "com.nvidia.geforcenow", ".*GeForce NOW.*" -> "…GeForce NOW…"
  function prettyPattern(p) {
    return String(p).replace(/\.\*/g, "…").replace(/\\(.)/g, "$1")
  }
  function dur(s) {
    if (!s) return "0m"
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60)
    return h > 0 ? h + "h " + m + "m" : (m > 0 ? m + "m" : s + "s")
  }

  readonly property string stateText: {
    if (!st) return svc && svc.status === "error" ? "Reader stopped" : "Starting"
    if (st.status === "permission-denied") return "No keyboard access (input group)"
    if (st.status !== "ready") return "No keyboard found"
    if (paused) return "Paused"
    if (ignored) return "Ignored app · " + st.app
    if (idle) return "Idle"
    return "Typing"
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // Leading spaces reserve room for the activity dot, so the number never shifts.
    text: "  " + (root.paused ? "󰏤 wpm" : root.n(root.liveWpm) + " wpm")
    fontSize: Style.font.caption
    horizontalMargin: 6
    // Always bright; only a missing number ("–") is faint.
    dimmed: root.liveWpm === null && !root.paused
    tooltipText: "Typing speed: " + root.stateText
    onPressed: function() { root.toggle() }

    // Green while typing, off when idle, paused or in an ignored app.
    Rectangle {
      width: Math.max(5, Math.round(Style.font.caption * 0.45))
      height: width
      radius: width / 2
      color: root.green
      visible: root.typing
      anchors.verticalCenter: parent.verticalCenter
      anchors.left: parent.left
      anchors.leftMargin: button.horizontalMargin + Math.round(Style.font.caption * 0.3)
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(390))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(12)

        // ---- header
        Column {
          width: parent.width
          spacing: Style.space(2)
          Text {
            text: "Live WPM"
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }
          Text {
            text: root.stateText.toUpperCase()
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: 1.2
            elide: Text.ElideRight
            width: parent.width
          }
        }

        // ---- big numbers
        Row {
          width: parent.width
          Repeater {
            model: [
              { label: "LIVE", value: root.liveWpm },
              { label: "SESSION", value: root.st ? root.st.session.wpm : null },
              { label: "SESSION PEAK", value: root.st ? root.st.session.peak : null }
            ]
            Column {
              required property var modelData
              width: column.width / 3
              Text {
                text: root.n(modelData.value)
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
                font.bold: true
              }
              Text {
                text: modelData.label
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }
            }
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---- history
        PanelSectionHeader { text: "HISTORY  ·  WPM / PEAK / TYPING TIME"; foreground: root.fg; fontFamily: root.fontFamily }

        Column {
          width: parent.width
          spacing: Style.space(4)
          Repeater {
            model: [
              { label: "Today", key: "today" },
              { label: "This week", key: "week" },
              { label: "This month", key: "month" },
              { label: "This year", key: "year" },
              { label: "All time", key: "all" }
            ]
            Row {
              required property var modelData
              readonly property var d: root.st ? root.st[modelData.key] : null
              width: column.width
              Text {
                width: parent.width * 0.4
                text: modelData.label
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
              Text {
                width: parent.width * 0.2
                text: root.n(d ? d.wpm : null)
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
                horizontalAlignment: Text.AlignRight
              }
              Text {
                width: parent.width * 0.2
                text: root.n(d ? d.peak : null)
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                horizontalAlignment: Text.AlignRight
              }
              Text {
                width: parent.width * 0.2
                text: root.dur(d ? d.active_s : 0)
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                horizontalAlignment: Text.AlignRight
              }
            }
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---- settings: the three that are worth a slider. The other rules
        // (minimum burst, end hesitation, peak window/minimum) are fixed here;
        // settings.json still takes them.
        PanelSectionHeader { text: "SETTINGS  ·  HISTORY IS RECALCULATED"; foreground: root.fg; fontFamily: root.fontFamily }

        Repeater {
          model: [
            { key: "pause_cutoff_ms", label: "Pause cutoff", hint: "longer gaps are not typing", step: 50 },
            { key: "end_trim_keys", label: "End trim", hint: "hesitation before the last keys", step: 1 },
            { key: "live_window_s", label: "Live window", hint: "recent typing in the live number", step: 5 }
          ]
          Column {
            id: settingRow
            required property var modelData
            readonly property var range: root.ranges ? root.ranges[modelData.key] : [0, 1, 0]
            readonly property real current: root.cfg ? root.cfg[modelData.key] : range[2]
            function snap(v) {
              var s = modelData.step
              return Math.max(range[0], Math.min(range[1], Math.round(v / s) * s))
            }
            width: column.width
            spacing: Style.space(2)
            Item {
              width: parent.width
              implicitHeight: labelText.implicitHeight
              Text {
                id: labelText
                text: settingRow.modelData.label + "  "
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                Text {
                  anchors.left: parent.right
                  anchors.baseline: parent.baseline
                  text: settingRow.modelData.hint
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
              Text {
                anchors.right: parent.right
                text: root.fmt(settingRow.modelData.key, settingRow.snap(slider.liveValue))
                  + (settingRow.current === settingRow.range[2] ? "" : "  ·  def " + root.fmt(settingRow.modelData.key, settingRow.range[2]))
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
              }
            }
            PanelSlider {
              id: slider
              width: parent.width
              bar: root.bar
              minimum: settingRow.range[0]
              maximum: settingRow.range[1]
              step: settingRow.modelData.step
              integer: true
              value: settingRow.current
              enabled: root.st !== null
              onReleased: function(v) {
                var snapped = settingRow.snap(v)
                if (snapped !== settingRow.current && root.svc) root.svc.setSetting(settingRow.modelData.key, snapped)
              }
            }
          }
        }

        // ---- actions
        Row {
          width: parent.width
          spacing: Style.space(6)
          Button {
            width: (parent.width - 2 * parent.spacing) / 3
            text: root.paused ? "▶ Resume" : "Pause"
            fontSize: Style.font.bodySmall
            foreground: root.fg
            fontFamily: root.fontFamily
            bordered: true
            active: root.paused
            onClicked: if (root.svc) root.svc.togglePause()
          }
          Button {
            width: (parent.width - 2 * parent.spacing) / 3
            text: "Reset session"
            fontSize: Style.font.bodySmall
            foreground: root.fg
            fontFamily: root.fontFamily
            bordered: true
            onClicked: if (root.svc) root.svc.resetSession()
          }
          Button {
            width: (parent.width - 2 * parent.spacing) / 3
            text: "Reset defaults"
            fontSize: Style.font.bodySmall
            foreground: root.fg
            fontFamily: root.fontFamily
            bordered: true
            onClicked: if (root.svc) root.svc.resetDefaults()
          }
        }

        Button {
          width: parent.width
          text: "Open history in browser"
          iconText: "󰄨"
          fontSize: Style.font.bodySmall
          foreground: root.fg
          fontFamily: root.fontFamily
          bordered: true
          enabled: root.st !== null
          onClicked: { if (root.svc) root.svc.openReport(); root.close() }
        }

        PanelSeparator { foreground: root.fg }

        // ---- ignored apps (matched against app or window title)
        PanelSectionHeader { text: "IGNORED APPS  ·  APP OR WINDOW TITLE"; foreground: root.fg; fontFamily: root.fontFamily }

        Column {
          width: parent.width
          spacing: Style.space(2)
          Repeater {
            model: root.cfg ? root.cfg.ignore_apps : []
            Item {
              required property var modelData
              width: column.width
              implicitHeight: removeButton.height
              Text {
                anchors.left: parent.left
                anchors.right: removeButton.left
                anchors.verticalCenter: parent.verticalCenter
                text: root.prettyPattern(modelData)
                elide: Text.ElideRight
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
              PanelActionButton {
                id: removeButton
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                iconText: "󰅖"
                tooltipText: "Stop ignoring"
                foreground: root.fg
                hoverColor: root.bar ? root.bar.urgent : root.fg
                fontFamily: root.fontFamily
                onClicked: if (root.svc) root.svc.ignoreRemove(modelData)
              }
            }
          }
          Text {
            visible: root.cfg && root.cfg.ignore_apps.length === 0
            text: "Nothing ignored"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
        }

        Button {
          width: parent.width
          readonly property string app: root.st ? String(root.st.app || "") : ""
          text: app === "" ? "Ignore current app" : (root.ignored ? "Already ignored: " : "Ignore current app: ") + app
          fontSize: Style.font.bodySmall
          foreground: root.fg
          fontFamily: root.fontFamily
          bordered: true
          enabled: app !== "" && !root.ignored
          onClicked: if (root.svc) root.svc.ignoreCurrent()
        }
      }
    }
  }
}
