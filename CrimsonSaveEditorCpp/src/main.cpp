#include "MainWindow.h"

#include <QApplication>

int main(int argc, char* argv[]) {
    QApplication app(argc, argv);
    app.setApplicationName("Crimson Desert Save Editor");
    app.setOrganizationName("saveeditor");

    MainWindow window;
    window.show();

    if (argc >= 2) {
        QMetaObject::invokeMethod(
            &window,
            "openPath",
            Qt::QueuedConnection,
            Q_ARG(QString, QString::fromLocal8Bit(argv[1])));
    }

    return app.exec();
}
