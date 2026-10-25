import QtQuick 2.7

// Небольшая скруглённая кнопка с текстом (основная или обычная).
Rectangle
{
    id: btn
    property string label: ""
    property bool primary: false
    property color bg: "#0e639c"
    property color fg: "#ffffff"
    property color border2: "#454545"
    property color textColor: "#cccccc"
    property real fontSize: 12
    signal clicked()

    implicitWidth: lab.implicitWidth + 20
    implicitHeight: lab.implicitHeight + 10
    width: implicitWidth
    height: implicitHeight
    radius: 4
    color: primary ? (area.containsMouse ? Qt.lighter(bg, 1.15) : bg) : (area.containsMouse ? "#22ffffff" : "transparent")
    border.width: primary ? 0 : 1
    border.color: border2
    opacity: enabled ? 1.0 : 0.45

    Text { id: lab; anchors.centerIn: parent; text: btn.label; font.pixelSize: btn.fontSize; color: btn.primary ? btn.fg : btn.textColor }
    MouseArea { id: area; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: btn.clicked() }
}
