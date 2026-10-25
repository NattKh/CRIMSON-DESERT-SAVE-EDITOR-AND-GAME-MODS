#pragma once

#include <QMainWindow>

#include "save_parser_cpp.h"

class QDragEnterEvent;
class QLabel;
class QLineEdit;
class QPlainTextEdit;
class QProgressBar;
class QDropEvent;
class QTreeWidget;
class QTreeWidgetItem;

class MainWindow final : public QMainWindow {
    Q_OBJECT

public:
    explicit MainWindow(QWidget* parent = nullptr);

private slots:
    void openFile();
    void openPath(const QString& path);
    void onTreeSelectionChanged();
    void applyFilter(const QString& text);

protected:
    void dragEnterEvent(QDragEnterEvent* event) override;
    void dropEvent(QDropEvent* event) override;

private:
    enum class NodeKind : int {
        StaticText = 0,
        TocEntry = 1,
        SchemaType = 2,
        SchemaField = 3,
        Object = 4,
        Field = 5,
    };

    struct TreePayload {
        QString title;
        NodeKind kind = NodeKind::StaticText;
        quintptr ref = 0;
        QString details;
        int startOffset = -1;
        int endOffset = -1;
    };

    void buildUi();
    void populateTree(const SaveParserCpp::ParseResult& result);
    void addSchemaNodes(QTreeWidgetItem* parent, const SaveParserCpp::ParseResult& result);
    void addTocNodes(QTreeWidgetItem* parent, const SaveParserCpp::ParseResult& result);
    void addObjectNodes(QTreeWidgetItem* parent, const SaveParserCpp::ParseResult& result);
    void addFieldNodes(QTreeWidgetItem* parent, const SaveParserCpp::GenericFieldValue& field);
    QTreeWidgetItem* makeNode(QTreeWidgetItem* parent, const TreePayload& payload);
    QString buildSummaryText(const SaveParserCpp::ParseResult& result) const;
    QString buildTocEntryText(const SaveParserCpp::TocEntry& entry) const;
    QString buildSchemaTypeText(const SaveParserCpp::TypeDef& type) const;
    QString buildSchemaFieldText(const SaveParserCpp::FieldDef& field) const;
    QString buildObjectText(const SaveParserCpp::ObjectBlock& object) const;
    QString buildFieldText(const SaveParserCpp::GenericFieldValue& field) const;
    QString buildHexText(int startOffset, int endOffset) const;
    bool filterNodeRecursive(QTreeWidgetItem* item, const QString& needle);

    QTreeWidget* m_tree = nullptr;
    QLineEdit* m_filter = nullptr;
    QPlainTextEdit* m_details = nullptr;
    QPlainTextEdit* m_hex = nullptr;
    QLabel* m_status = nullptr;
    QProgressBar* m_progress = nullptr;

    SaveParserCpp::ParseResult m_result;
    QString m_loadedPath;
};
