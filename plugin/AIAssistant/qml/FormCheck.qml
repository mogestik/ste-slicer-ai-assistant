import QtQuick 2.7

// Флажок для формы настроек.
Item
{
    id: check
    property var c
    property real k: 1
    property string label: ""
    property string hint: ""
    property bool checked: false
    implicitHeight: Math.max(box.height, labelCol.implicitHeight)

    Rectangle
    {
        id: box
        width: 16 * check.k
        height: width
        y: 2 * check.k
        radius: 3 * check.k
        color: check.checked ? check.c.accentBtn : check.c.surface
        border.width: 1
        border.color: check.checked ? check.c.accentBtn : check.c.border
        Text
        {
            anchors.centerIn: parent
            visible: check.checked
            text: ""
            font.family: "Segoe MDL2 Assets"
            font.pixelSize: Math.round(10 * check.k)
            color: "white"
        }
    }
    Column
    {
        id: labelCol
        anchors { left: box.right; leftMargin: 8 * check.k; right: parent.right }
        spacing: 1
        Text { width: parent.width; wrapMode: Text.Wrap; text: check.label; color: check.c.text; font.pixelSize: Math.round(12 * check.k) }
        Text { visible: check.hint !== ""; width: parent.width; wrapMode: Text.Wrap; text: check.hint; color: check.c.text2
               font.pixelSize: Math.round(11 * check.k) }
    }
    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: check.checked = !check.checked }
}
