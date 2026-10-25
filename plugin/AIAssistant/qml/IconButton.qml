import QtQuick 2.7

// Плоская кнопка-иконка: глиф шрифта Segoe MDL2 Assets (есть в Windows 10+).
Rectangle
{
    id: btn
    property string glyph: ""
    property color fg: "#cccccc"
    property color hoverColor: "#40ffffff"
    property real size: 28
    property bool active: false
    property string tip: ""
    signal clicked()

    width: size
    height: size
    radius: Math.round(size * 0.18)
    color: area.pressed || area.containsMouse ? hoverColor : (active ? hoverColor : "transparent")
    opacity: enabled ? 1.0 : 0.4

    Text
    {
        anchors.centerIn: parent
        text: btn.glyph
        font.family: "Segoe MDL2 Assets"
        font.pixelSize: Math.round(btn.size * 0.5)
        color: btn.fg
    }
    MouseArea
    {
        id: area
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: btn.clicked()
    }
    Rectangle   // подсказка
    {
        visible: area.containsMouse && btn.tip !== "" && tipTimer.shown
        z: 10
        y: parent.height + 4
        x: Math.min(0, (btn.parent ? btn.parent.width : 0) - width - btn.x)
        width: tipText.implicitWidth + 12
        height: tipText.implicitHeight + 8
        radius: 3
        color: "#303031"
        border.color: "#454545"
        Text { id: tipText; anchors.centerIn: parent; text: btn.tip; color: "#e6e6e6"; font.pixelSize: 11 }
    }
    Timer { id: tipTimer; property bool shown: false; interval: 500; running: area.containsMouse; onRunningChanged: shown = false; onTriggered: shown = true }
}
