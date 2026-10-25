import QtQuick 2.7

// Однострочное поле с подписью для формы настроек (цвета берём из палитры панели `c`).
Column
{
    id: field
    property var c
    property real k: 1
    property string label: ""
    property string hint: ""
    property string placeholder: ""
    property alias text: input.text
    property bool password: false
    property bool numeric: false
    spacing: 3 * k

    Text { text: field.label; color: field.c.text; font.pixelSize: Math.round(12 * field.k); font.bold: true }
    Rectangle
    {
        width: field.width
        height: input.implicitHeight + 12 * field.k
        radius: 4 * field.k
        color: field.c.surface
        border.width: 1
        border.color: input.activeFocus ? field.c.accent : field.c.border
        TextInput
        {
            id: input
            x: 8 * field.k
            width: parent.width - 16 * field.k
            anchors.verticalCenter: parent.verticalCenter
            clip: true
            color: field.c.text
            selectionColor: field.c.accent
            selectByMouse: true
            font.pixelSize: Math.round(13 * field.k)
            echoMode: field.password && !input.activeFocus ? TextInput.Password : TextInput.Normal
            inputMethodHints: field.numeric ? Qt.ImhFormattedNumbersOnly : Qt.ImhNone
            Text
            {
                visible: input.text.length === 0 && !input.activeFocus
                text: field.placeholder
                color: field.c.text2
                font.pixelSize: input.font.pixelSize
            }
        }
    }
    Text
    {
        visible: field.hint !== ""
        width: field.width
        wrapMode: Text.Wrap
        text: field.hint
        color: field.c.text2
        font.pixelSize: Math.round(11 * field.k)
    }
}
