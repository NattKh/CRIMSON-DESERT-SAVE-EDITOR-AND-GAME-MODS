#include "MainWindow.h"

#include <QApplication>
#include <QDragEnterEvent>
#include <QFileDialog>
#include <QFont>
#include <QHeaderView>
#include <QLabel>
#include <QLineEdit>
#include <QMenuBar>
#include <QMessageBox>
#include <QMimeData>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QSignalBlocker>
#include <QSplitter>
#include <QStatusBar>
#include <QTreeWidget>
#include <QUrl>
#include <QVBoxLayout>
#include <QWidget>

#include <algorithm>
#include <chrono>
#include <iomanip>
#include <sstream>

namespace {

constexpr int kRoleDetails = Qt::UserRole;
constexpr int kRoleStartOffset = Qt::UserRole + 1;
constexpr int kRoleEndOffset = Qt::UserRole + 2;
constexpr int kRoleNodeKind = Qt::UserRole + 3;
constexpr int kRoleNodeRef = Qt::UserRole + 4;

QString ToQString(const std::string& value) {
    return QString::fromUtf8(value.c_str(), static_cast<int>(value.size()));
}

QString HexOffsetLabel(int startOffset, int endOffset) {
    if (startOffset < 0 || endOffset < 0) {
        return {};
    }
    return QString("0x%1 .. 0x%2")
        .arg(startOffset, 0, 16)
        .arg(endOffset, 0, 16);
}

} // namespace

MainWindow::MainWindow(QWidget* parent)
    : QMainWindow(parent) {
    buildUi();
}

void MainWindow::buildUi() {
    setWindowTitle("Crimson Desert Save Editor");
    resize(1680, 980);
    setAcceptDrops(true);

    auto* fileMenu = menuBar()->addMenu("&File");
    auto* openAction = fileMenu->addAction("&Open...");
    connect(openAction, &QAction::triggered, this, &MainWindow::openFile);
    fileMenu->addSeparator();
    auto* quitAction = fileMenu->addAction("E&xit");
    connect(quitAction, &QAction::triggered, this, &QWidget::close);

    auto* central = new QWidget(this);
    auto* outerLayout = new QVBoxLayout(central);
    outerLayout->setContentsMargins(6, 6, 6, 6);
    outerLayout->setSpacing(6);

    m_filter = new QLineEdit(this);
    m_filter->setPlaceholderText("Quick filter tree...");
    connect(m_filter, &QLineEdit::textChanged, this, &MainWindow::applyFilter);
    outerLayout->addWidget(m_filter);

    auto* mainSplitter = new QSplitter(Qt::Horizontal, this);
    outerLayout->addWidget(mainSplitter, 1);

    m_tree = new QTreeWidget(this);
    m_tree->setHeaderHidden(true);
    m_tree->setAlternatingRowColors(true);
    m_tree->header()->setStretchLastSection(true);
    m_tree->setMinimumWidth(320);
    m_tree->setUniformRowHeights(true);
    m_tree->setAnimated(false);
    m_tree->setExpandsOnDoubleClick(true);
    connect(m_tree, &QTreeWidget::itemSelectionChanged, this, &MainWindow::onTreeSelectionChanged);
    mainSplitter->addWidget(m_tree);

    auto* rightSplitter = new QSplitter(Qt::Horizontal, this);
    mainSplitter->addWidget(rightSplitter);

    m_details = new QPlainTextEdit(this);
    m_details->setReadOnly(true);
    m_details->setPlaceholderText("Parsed details");
    rightSplitter->addWidget(m_details);

    m_hex = new QPlainTextEdit(this);
    m_hex->setReadOnly(true);
    m_hex->setPlaceholderText("Hex view");
    QFont mono(QStringLiteral("Consolas"));
    mono.setStyleHint(QFont::Monospace);
    m_details->setFont(mono);
    m_hex->setFont(mono);
    rightSplitter->addWidget(m_hex);

    mainSplitter->setStretchFactor(0, 1);
    mainSplitter->setStretchFactor(1, 3);
    rightSplitter->setStretchFactor(0, 1);
    rightSplitter->setStretchFactor(1, 1);

    setCentralWidget(central);

    m_status = new QLabel("Ready", this);
    statusBar()->addPermanentWidget(m_status, 1);
    m_progress = new QProgressBar(this);
    m_progress->setRange(0, 100);
    m_progress->setValue(0);
    m_progress->setTextVisible(true);
    m_progress->setFormat("%p%");
    m_progress->setFixedWidth(220);
    m_progress->hide();
    statusBar()->addPermanentWidget(m_progress, 0);
}

void MainWindow::openFile() {
    const QString path = QFileDialog::getOpenFileName(
        this,
        "Open Crimson Desert save",
        QString(),
        "Save files (*.save *.bin *.out);;All files (*.*)");
    if (!path.isEmpty()) {
        openPath(path);
    }
}

void MainWindow::openPath(const QString& path) {
    try {
        QApplication::setOverrideCursor(Qt::WaitCursor);
        m_progress->setRange(0, 100);
        m_progress->setValue(0);
        m_progress->setFormat("%p%");
        m_progress->show();
        m_status->setText(QString("Loading %1 | reading file...").arg(path));
        QApplication::processEvents(QEventLoop::ExcludeUserInputEvents);

        int lastPercent = -1;
        QString lastStatus;
        auto updateProgress = [&](const SaveParserCpp::ProgressInfo& info) {
            int percent = 0;
            if (info.stage == "Reading file") {
                percent = 5;
            } else if (info.stage == "Decrypting payload") {
                percent = 5 + ((info.total == 0) ? 0 : static_cast<int>((10.0 * info.current) / info.total));
            } else if (info.stage == "Inflating payload") {
                percent = 15 + ((info.total == 0) ? 0 : static_cast<int>((10.0 * info.current) / info.total));
            } else if (info.stage == "Parsing schema") {
                percent = 25 + ((info.total == 0) ? 0 : static_cast<int>((15.0 * info.current) / info.total));
            } else if (info.stage == "Parsing TOC") {
                percent = 40 + ((info.total == 0) ? 0 : static_cast<int>((10.0 * info.current) / info.total));
            } else if (info.stage == "Decoding objects") {
                percent = 50 + ((info.total == 0) ? 0 : static_cast<int>((40.0 * info.current) / info.total));
            } else {
                percent = 0;
            }
            percent = std::clamp(percent, 0, 95);

            QString stageText = QString::fromUtf8(info.stage.c_str(), static_cast<int>(info.stage.size()));
            if (info.total > 1) {
                stageText += QString(" (%1/%2)").arg(info.current).arg(info.total);
            }
            const QString statusText = QString("Loading %1 | %2").arg(path, stageText);

            if (percent != lastPercent || statusText != lastStatus) {
                lastPercent = percent;
                lastStatus = statusText;
                m_progress->setValue(percent);
                m_status->setText(statusText);
                QApplication::processEvents(QEventLoop::ExcludeUserInputEvents);
            }
        };

        const auto parseStart = std::chrono::steady_clock::now();
        m_result = SaveParserCpp::ParseFile(path.toStdString(), "", updateProgress);
        const auto parseEnd = std::chrono::steady_clock::now();
        m_loadedPath = path;
        m_progress->setValue(92);
        m_status->setText(QString("Loading %1 | building tree...").arg(path));
        QApplication::processEvents(QEventLoop::ExcludeUserInputEvents);
        const auto treeStart = std::chrono::steady_clock::now();
        populateTree(m_result);
        const auto treeEnd = std::chrono::steady_clock::now();
        m_hex->clear();

        const auto decodedObjectBytes = static_cast<double>(m_result.stats.decoded_object_bytes);
        const auto objectBytes = static_cast<double>(m_result.stats.object_bytes ? m_result.stats.object_bytes : 1);
        const auto fieldPct = (m_result.stats.present_fields == 0)
            ? 100.0
            : (100.0 * static_cast<double>(m_result.stats.decoded_present_fields) / static_cast<double>(m_result.stats.present_fields));
        const auto bytePct = 100.0 * decodedObjectBytes / objectBytes;
        const auto parseMs = std::chrono::duration_cast<std::chrono::milliseconds>(parseEnd - parseStart).count();
        const auto treeMs = std::chrono::duration_cast<std::chrono::milliseconds>(treeEnd - treeStart).count();

        m_status->setText(
            QString("%1 | %2 | raw=%3 | types=%4 | toc=%5 | parsed=%6% bytes, %7% fields | parse=%8 ms | tree=%9 ms")
                .arg(path)
                .arg(ToQString(m_result.input_kind))
                .arg(m_result.raw_size)
                .arg(m_result.schema.type_count)
                .arg(m_result.toc.entry_count)
                .arg(bytePct, 0, 'f', 2)
                .arg(fieldPct, 0, 'f', 2)
                .arg(parseMs)
                .arg(treeMs));
        m_progress->setValue(100);
        m_progress->hide();
    } catch (const std::exception& exc) {
        m_progress->hide();
        QMessageBox::critical(this, "Open failed", QString::fromUtf8(exc.what()));
    }
    if (QApplication::overrideCursor()) {
        QApplication::restoreOverrideCursor();
    }
}

void MainWindow::dragEnterEvent(QDragEnterEvent* event) {
    if (!event || !event->mimeData()) {
        return;
    }
    const auto urls = event->mimeData()->urls();
    if (urls.isEmpty()) {
        return;
    }
    for (const auto& url : urls) {
        if (url.isLocalFile()) {
            event->acceptProposedAction();
            return;
        }
    }
}

void MainWindow::dropEvent(QDropEvent* event) {
    if (!event || !event->mimeData()) {
        return;
    }
    const auto urls = event->mimeData()->urls();
    for (const auto& url : urls) {
        if (!url.isLocalFile()) {
            continue;
        }
        const QString path = url.toLocalFile();
        if (!path.isEmpty()) {
            event->acceptProposedAction();
            openPath(path);
            return;
        }
    }
}

QTreeWidgetItem* MainWindow::makeNode(QTreeWidgetItem* parent, const TreePayload& payload) {
    auto* item = parent ? new QTreeWidgetItem(parent) : new QTreeWidgetItem(m_tree);
    item->setText(0, payload.title);
    item->setData(0, kRoleDetails, payload.details);
    item->setData(0, kRoleStartOffset, payload.startOffset);
    item->setData(0, kRoleEndOffset, payload.endOffset);
    item->setData(0, kRoleNodeKind, static_cast<int>(payload.kind));
    item->setData(0, kRoleNodeRef, static_cast<qulonglong>(payload.ref));
    return item;
}

QString MainWindow::buildSummaryText(const SaveParserCpp::ParseResult& result) const {
    std::ostringstream oss;
    oss << "Input kind: " << result.input_kind << "\n";
    oss << "Path: " << result.input_path << "\n";
    oss << "Raw size: " << result.raw_size << "\n";
    if (result.container.present) {
        oss << "\nContainer\n";
        oss << "  Version: " << result.container.version << "\n";
        oss << "  Flags: " << result.container.flags << "\n";
        oss << "  float_flag: " << result.container.float_flag << "\n";
        oss << "  field_0C: " << result.container.field_0C << "\n";
        oss << "  field_10: " << result.container.field_10 << "\n";
        oss << "  uncompressed_size: " << result.container.uncompressed_size << "\n";
        oss << "  payload_size: " << result.container.payload_size << "\n";
        oss << "  nonce: " << result.container.nonce_hex << "\n";
        oss << "  hmac: " << result.container.hmac_hex << "\n";
        oss << "  hmac_ok: " << (result.container.hmac_ok ? "true" : "false") << "\n";
    }
    oss << "\nSchema\n";
    oss << "  type_count: " << result.schema.type_count << "\n";
    oss << "  root_type: " << result.schema.root_type << "\n";
    oss << "  schema_end: 0x" << std::hex << result.schema.schema_end << std::dec << "\n";
    oss << "\nTOC\n";
    oss << "  entry_count: " << result.toc.entry_count << "\n";
    oss << "  stream_size: " << result.toc.stream_size << "\n";
    oss << "\nStats\n";
    oss << "  object_bytes: " << result.stats.object_bytes << "\n";
    oss << "  decoded_object_bytes: " << result.stats.decoded_object_bytes << "\n";
    oss << "  present_fields: " << result.stats.present_fields << "\n";
    oss << "  decoded_present_fields: " << result.stats.decoded_present_fields << "\n";
    return QString::fromUtf8(oss.str().c_str());
}

QString MainWindow::buildTocEntryText(const SaveParserCpp::TocEntry& entry) const {
    return QString("class_index=%1\nsentinel1=%2\nsentinel2=%3\ndata_offset=0x%4\ndata_size=0x%5")
        .arg(entry.class_index)
        .arg(entry.sentinel1)
        .arg(entry.sentinel2)
        .arg(entry.data_offset, 0, 16)
        .arg(entry.data_size, 0, 16);
}

QString MainWindow::buildSchemaTypeText(const SaveParserCpp::TypeDef& type) const {
    return QString("Type %1\nfields=%2\nrange=%3")
        .arg(ToQString(type.name))
        .arg(type.fields.size())
        .arg(HexOffsetLabel(static_cast<int>(type.start_offset), static_cast<int>(type.end_offset)));
}

QString MainWindow::buildSchemaFieldText(const SaveParserCpp::FieldDef& field) const {
    return QString("Field %1\nType: %2\nmeta_kind=%3\nmeta_size=%4\nmeta_aux=%5")
        .arg(ToQString(field.name))
        .arg(ToQString(field.type_name))
        .arg(field.meta_kind)
        .arg(field.meta_size)
        .arg(field.meta_aux);
}

QString MainWindow::buildObjectText(const SaveParserCpp::ObjectBlock& object) const {
    std::ostringstream oss;
    oss << object.class_name << "\n";
    oss << "entry_index: " << object.entry_index << "\n";
    oss << "class_index: " << object.class_index << "\n";
    oss << "data_offset: 0x" << std::hex << object.data_offset << "\n";
    oss << "data_size: 0x" << object.data_size << std::dec << " (" << object.data_size << ")\n";
    oss << "mask_byte_count: " << object.mask_byte_count << "\n";
    oss << "reserved_u32: " << object.reserved_u32 << "\n";
    oss << "field_count: " << object.fields.size() << "\n";
    oss << "undecoded_ranges: " << object.undecoded_ranges.size() << "\n";
    return QString::fromUtf8(oss.str().c_str());
}

QString MainWindow::buildFieldText(const SaveParserCpp::GenericFieldValue& field) const {
    std::ostringstream oss;
    oss << field.name << "\n";
    oss << "type_name: " << field.type_name << "\n";
    oss << "meta_kind: " << field.meta_kind << "\n";
    oss << "meta_size: " << field.meta_size << "\n";
    oss << "meta_aux: " << field.meta_aux << "\n";
    oss << "present: " << (field.present ? "true" : "false") << "\n";
    oss << "decode_kind: " << field.decode_kind << "\n";
    oss << "start_offset: 0x" << std::hex << field.start_offset << "\n";
    oss << "end_offset: 0x" << field.end_offset << std::dec << "\n";
    oss << "value_repr: " << field.value_repr << "\n";
    if (!field.note.empty()) {
        oss << "note: " << field.note << "\n";
    }
    if (!field.child_type_name.empty()) {
        oss << "child_type: " << field.child_type_name << "\n";
        oss << "child_payload_offset: 0x" << std::hex << field.child_payload_offset << std::dec << "\n";
    }
    if (field.list_count) {
        oss << "list_count: " << field.list_count << "\n";
    }
    if (!field.child_fields.empty()) {
        oss << "child_field_count: " << field.child_fields.size() << "\n";
    }
    if (!field.list_elements.empty()) {
        oss << "list_element_count: " << field.list_elements.size() << "\n";
    }
    return QString::fromUtf8(oss.str().c_str());
}

QString MainWindow::buildHexText(int startOffset, int endOffset) const {
    if (startOffset < 0 || endOffset <= startOffset || m_result.raw_blob.empty()) {
        return {};
    }

    const int rawSize = static_cast<int>(m_result.raw_blob.size());
    const int start = std::clamp(startOffset, 0, rawSize);
    const int end = std::clamp(endOffset, 0, rawSize);
    if (end <= start) {
        return {};
    }

    constexpr int kMaxBytes = 4096;
    const int shownEnd = std::min(end, start + kMaxBytes);
    std::ostringstream oss;
    oss << "Range: " << HexOffsetLabel(start, end).toStdString() << "\n";
    if (shownEnd < end) {
        oss << "Showing first " << (shownEnd - start) << " of " << (end - start) << " bytes\n";
    }
    oss << "\n";

    for (int lineStart = start; lineStart < shownEnd; lineStart += 16) {
        const int lineEnd = std::min(lineStart + 16, shownEnd);
        oss << std::hex << std::setw(8) << std::setfill('0') << lineStart << "  ";
        for (int i = 0; i < 16; ++i) {
            const int idx = lineStart + i;
            if (idx < lineEnd) {
                oss << std::setw(2) << static_cast<unsigned>(m_result.raw_blob[static_cast<size_t>(idx)]) << ' ';
            } else {
                oss << "   ";
            }
            if (i == 7) {
                oss << ' ';
            }
        }
        oss << " |";
        for (int idx = lineStart; idx < lineEnd; ++idx) {
            const unsigned char c = m_result.raw_blob[static_cast<size_t>(idx)];
            oss << ((c >= 32 && c <= 126) ? static_cast<char>(c) : '.');
        }
        oss << "|\n";
    }
    return QString::fromUtf8(oss.str().c_str());
}

void MainWindow::addFieldNodes(QTreeWidgetItem* parent, const SaveParserCpp::GenericFieldValue& field) {
    if (!field.present) {
        return;
    }
    const QString title = QString("%1 : %2 [%3]").arg(ToQString(field.name), ToQString(field.type_name), ToQString(field.decode_kind));
    auto* node = makeNode(parent, TreePayload{
        title,
        NodeKind::Field,
        reinterpret_cast<quintptr>(&field),
        {},
        static_cast<int>(field.start_offset),
        static_cast<int>(field.end_offset),
    });

    for (const auto& child : field.child_fields) {
        addFieldNodes(node, child);
    }
    for (const auto& element : field.list_elements) {
        addFieldNodes(node, element);
    }
}

void MainWindow::addSchemaNodes(QTreeWidgetItem* parent, const SaveParserCpp::ParseResult& result) {
    for (const auto& type : result.schema.types) {
        auto* typeNode = makeNode(parent, TreePayload{
            QString("#%1 %2").arg(type.index).arg(ToQString(type.name)),
            NodeKind::SchemaType,
            reinterpret_cast<quintptr>(&type),
            {},
            static_cast<int>(type.start_offset),
            static_cast<int>(type.end_offset),
        });
        for (const auto& field : type.fields) {
            makeNode(typeNode, TreePayload{
                QString("%1 : %2 [k%3 s%4 a%5]")
                    .arg(ToQString(field.name))
                    .arg(ToQString(field.type_name))
                    .arg(field.meta_kind)
                    .arg(field.meta_size)
                    .arg(field.meta_aux),
                NodeKind::SchemaField,
                reinterpret_cast<quintptr>(&field),
                {},
                static_cast<int>(field.start_offset),
                static_cast<int>(field.end_offset),
            });
        }
    }
}

void MainWindow::addTocNodes(QTreeWidgetItem* parent, const SaveParserCpp::ParseResult& result) {
    for (const auto& entry : result.toc.entries) {
        makeNode(parent, TreePayload{
            QString("#%1 %2 @ 0x%3")
                .arg(entry.index)
                .arg(ToQString(entry.class_name))
                .arg(entry.data_offset, 0, 16),
            NodeKind::TocEntry,
            reinterpret_cast<quintptr>(&entry),
            {},
            static_cast<int>(entry.data_offset),
            static_cast<int>(entry.data_offset + entry.data_size),
        });
    }
}

void MainWindow::addObjectNodes(QTreeWidgetItem* parent, const SaveParserCpp::ParseResult& result) {
    for (const auto& object : result.objects) {
        auto* objectNode = makeNode(parent, TreePayload{
            QString("#%1 %2 @ 0x%3")
                .arg(object.entry_index)
                .arg(ToQString(object.class_name))
                .arg(object.data_offset, 0, 16),
            NodeKind::Object,
            reinterpret_cast<quintptr>(&object),
            {},
            static_cast<int>(object.data_offset),
            static_cast<int>(object.data_offset + object.data_size),
        });
        for (const auto& field : object.fields) {
            addFieldNodes(objectNode, field);
        }
    }
}

void MainWindow::populateTree(const SaveParserCpp::ParseResult& result) {
    QSignalBlocker blocker(m_tree);
    m_tree->setUpdatesEnabled(false);
    m_tree->clear();

    auto* summaryNode = makeNode(nullptr, TreePayload{"Summary", NodeKind::StaticText, 0, buildSummaryText(result), -1, -1});
    summaryNode->setExpanded(true);

    auto* schemaNode = makeNode(nullptr, TreePayload{"Schema", NodeKind::StaticText, 0, "Schema types", -1, -1});
    addSchemaNodes(schemaNode, result);

    auto* tocNode = makeNode(nullptr, TreePayload{"TOC", NodeKind::StaticText, 0, "TOC entries", -1, -1});
    addTocNodes(tocNode, result);

    auto* objectsNode = makeNode(nullptr, TreePayload{"Objects", NodeKind::StaticText, 0, "Decoded object blocks", -1, -1});
    addObjectNodes(objectsNode, result);
    objectsNode->setExpanded(true);

    m_tree->setCurrentItem(summaryNode);
    m_tree->setUpdatesEnabled(true);
    onTreeSelectionChanged();
}

void MainWindow::onTreeSelectionChanged() {
    const auto items = m_tree->selectedItems();
    if (items.isEmpty()) {
        return;
    }
    auto* item = items.front();
    QString text = item->data(0, kRoleDetails).toString();
    const int startOffset = item->data(0, kRoleStartOffset).toInt();
    const int endOffset = item->data(0, kRoleEndOffset).toInt();
    const auto kind = static_cast<NodeKind>(item->data(0, kRoleNodeKind).toInt());
    const auto ref = static_cast<quintptr>(item->data(0, kRoleNodeRef).toULongLong());

    switch (kind) {
    case NodeKind::TocEntry:
        if (ref != 0) {
            text = buildTocEntryText(*reinterpret_cast<const SaveParserCpp::TocEntry*>(ref));
        }
        break;
    case NodeKind::SchemaType:
        if (ref != 0) {
            text = buildSchemaTypeText(*reinterpret_cast<const SaveParserCpp::TypeDef*>(ref));
        }
        break;
    case NodeKind::SchemaField:
        if (ref != 0) {
            text = buildSchemaFieldText(*reinterpret_cast<const SaveParserCpp::FieldDef*>(ref));
        }
        break;
    case NodeKind::Object:
        if (ref != 0) {
            text = buildObjectText(*reinterpret_cast<const SaveParserCpp::ObjectBlock*>(ref));
        }
        break;
    case NodeKind::Field:
        if (ref != 0) {
            text = buildFieldText(*reinterpret_cast<const SaveParserCpp::GenericFieldValue*>(ref));
        }
        break;
    case NodeKind::StaticText:
    default:
        break;
    }

    if (startOffset >= 0) {
        text += QString("\n\nRange: %1").arg(HexOffsetLabel(startOffset, endOffset));
    }
    m_details->setPlainText(text);
    m_hex->setPlainText(buildHexText(startOffset, endOffset));
}

bool MainWindow::filterNodeRecursive(QTreeWidgetItem* item, const QString& needle) {
    bool selfMatch = item->text(0).contains(needle, Qt::CaseInsensitive);
    bool childMatch = false;
    for (int i = 0; i < item->childCount(); ++i) {
        childMatch = filterNodeRecursive(item->child(i), needle) || childMatch;
    }
    const bool visible = needle.isEmpty() || selfMatch || childMatch;
    item->setHidden(!visible);
    if (visible && !needle.isEmpty() && childMatch) {
        item->setExpanded(true);
    }
    return visible;
}

void MainWindow::applyFilter(const QString& text) {
    for (int i = 0; i < m_tree->topLevelItemCount(); ++i) {
        filterNodeRecursive(m_tree->topLevelItem(i), text);
    }
}
