import QtQuick 2.7

// Маленький индикатор загрузки: бледное кольцо и бегущая по нему дуга.
Item
{
    id: spin
    property real size: 14
    property color color: "#3794ff"
    property bool running: true
    width: size
    height: size

    Canvas
    {
        id: canvas
        anchors.fill: parent
        antialiasing: true
        onPaint:
        {
            var ctx = getContext("2d");
            ctx.reset();
            var r = width / 2 - 1.5;
            ctx.lineWidth = Math.max(1.5, width / 8);
            ctx.strokeStyle = Qt.rgba(spin.color.r, spin.color.g, spin.color.b, 0.25);
            ctx.beginPath(); ctx.arc(width / 2, height / 2, r, 0, Math.PI * 2); ctx.stroke();
            ctx.strokeStyle = spin.color;
            ctx.beginPath(); ctx.arc(width / 2, height / 2, r, -Math.PI / 2, Math.PI * 0.5); ctx.stroke();
        }
        onWidthChanged: requestPaint()
        RotationAnimator on rotation { running: spin.running && spin.visible; from: 0; to: 360; duration: 900; loops: Animation.Infinite }
    }
    onColorChanged: canvas.requestPaint()
}
