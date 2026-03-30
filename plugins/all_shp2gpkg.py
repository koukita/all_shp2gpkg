import os  # ファイルやフォルダ操作
import re  # 文字列の置換（正規表現）
import processing  # QGISの処理ツールを使う

# QGISのGUI部品
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QProgressDialog
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import Qt, QCoreApplication

# QGISのコア機能
from qgis.core import (
    QgsProject,
    QgsLayerTreeLayer,
    QgsLayerTreeGroup,
    QgsVectorLayer,
    QgsVectorFileWriter
)

# アイコン用（resources）
from . import resources


class AllShp2Gpkg:

    def __init__(self, iface):
        # iface = QGISの画面操作用インターフェース
        self.iface = iface
        self.action = None

    # =========================
    # GUI初期化（プラグイン起動時）
    # =========================
    def initGui(self):

        # プラグインフォルダ内のアイコンを取得
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")

        # ツールバーやメニューに表示するボタンを作成
        self.action = QAction(
            QIcon(icon_path),
            "all_shp2gpkg",
            self.iface.mainWindow()
        )

        # ボタンが押されたときに run() を実行
        self.action.triggered.connect(self.run)

        # ツールバーに追加
        self.iface.addToolBarIcon(self.action)

        # メニューに追加
        self.iface.addPluginToMenu("all_shp2gpkg", self.action)

    # =========================
    # プラグイン終了時
    # =========================
    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu("all_shp2gpkg", self.action)

    # =========================
    # レイヤを順番どおり取得する関数
    # =========================
    def get_all_layer_nodes(self, group):
        nodes = []
        for child in group.children():

            # 通常のレイヤ
            if isinstance(child, QgsLayerTreeLayer):
                nodes.append(child)

            # グループの場合は中身を再帰的に取得
            elif isinstance(child, QgsLayerTreeGroup):
                nodes.extend(self.get_all_layer_nodes(child))

        return nodes

    # =========================
    # GeoPackage用の安全な名前を作る
    # =========================
    def safe_layer_name(self, name):
        # 記号などを「_」に変換
        name = re.sub(r'\W+', '_', name)

        # 先頭が数字だとエラーになるので回避
        if re.match(r'^\d', name):
            name = "layer_" + name

        return name

    # =========================
    # メイン処理
    # =========================
    def run(self):

        # -------------------------
        # 実行確認ダイアログ
        # -------------------------
        msg = QMessageBox(self.iface.mainWindow())
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("確認")
        msg.setText(
            "レイヤのシェープファイルをすべてGeopackageに変換します。\n"
            "レイヤの順序、スタイルは引き継がれます。\n"
            "リレーションは引き継がれません。\n\n"
            "実行していいですか？"
        )

        msg.setStandardButtons(
            QMessageBox.StandardButton.Ok |
            QMessageBox.StandardButton.Cancel
        )

        # キャンセルなら終了
        if msg.exec() == QMessageBox.StandardButton.Cancel:
            return

        # 現在のQGISプロジェクトを取得
        project = QgsProject.instance()

        # レイヤツリー（レイヤ一覧）
        root = project.layerTreeRoot()

        # 変換対象の情報を格納するリスト
        tasks = []

        # -------------------------
        # レイヤ収集
        # -------------------------
        for node in self.get_all_layer_nodes(root):

            layer = node.layer()

            # 無効なレイヤはスキップ
            if not layer:
                continue

            # ベクタレイヤのみ対象
            if not isinstance(layer, QgsVectorLayer):
                continue

            # シェープファイル（ogr）のみ対象
            if layer.providerType() != "ogr":
                continue

            if not layer.source().lower().endswith(".shp"):
                continue

            # レイヤID（内部識別用）
            layer_id = layer.id()

            # 親グループと位置
            parent = node.parent()
            index = parent.children().index(node)

            # -------------------------
            # スタイル情報を保存
            # -------------------------
            renderer = layer.renderer().clone()
            labeling = layer.labeling().clone() if layer.labeling() else None
            labels_enabled = layer.labelsEnabled()
            form_config = layer.editFormConfig()

            fields = layer.fields()
            editor_setups = [layer.editorWidgetSetup(i) for i in range(fields.count())]

            opacity = layer.opacity()
            blend_mode = layer.blendMode()

            # -------------------------
            # 出力パス作成
            # -------------------------
            shp_path = layer.source().split("|")[0]
            folder = os.path.dirname(shp_path)
            base_name = os.path.splitext(os.path.basename(shp_path))[0]

            # 同じ場所にGPKGを作成
            gpkg_path = os.path.join(folder, base_name + ".gpkg")

            # 安全なレイヤ名
            safe_name = self.safe_layer_name(layer.name())

            # まとめて保存
            tasks.append({
                "layer": layer,
                "layer_id": layer_id,
                "original_name": layer.name(),
                "renderer": renderer,
                "labeling": labeling,
                "labels_enabled": labels_enabled,
                "form_config": form_config,
                "editor_setups": editor_setups,
                "opacity": opacity,
                "blend_mode": blend_mode,
                "parent": parent,
                "index": index,
                "gpkg_path": gpkg_path,
                "safe_name": safe_name
            })

        # 対象が無い場合
        if not tasks:
            QMessageBox.information(self.iface.mainWindow(), "情報", "対象のシェープファイルがありません")
            return

        # 進捗最大値（変換＋置換）
        total_steps = len(tasks) * 2

        # -------------------------
        # 進捗ダイアログ
        # -------------------------
        progress = QProgressDialog("開始中...", "キャンセル", 0, total_steps, self.iface.mainWindow())
        progress.setWindowTitle("処理中")
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setMinimumDuration(0)

        step = 0

        # -------------------------
        # ① シェープ → GPKG変換
        # -------------------------
        for t in tasks:

            # キャンセルチェック
            if progress.wasCanceled():
                return

            # 表示更新
            progress.setLabelText(f"変換中：{t['original_name']}")
            progress.setValue(step)

            # UIフリーズ防止（重要）
            QCoreApplication.processEvents()

            # ジオメトリ修正（壊れている可能性に対応）
            fixed = processing.run("native:fixgeometries", {
                'INPUT': t["layer"],
                'OUTPUT': 'memory:'
            })['OUTPUT']

            # 既存GPKG削除
            if os.path.exists(t["gpkg_path"]):
                os.remove(t["gpkg_path"])

            # 保存設定
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = t["safe_name"]
            options.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile

            # 書き込み実行
            result = QgsVectorFileWriter.writeAsVectorFormatV3(
                fixed,
                t["gpkg_path"],
                project.transformContext(),
                options
            )[0]

            # 成功・失敗を記録
            t["failed"] = (result != QgsVectorFileWriter.NoError)

            step += 1

        # -------------------------
        # ② レイヤ置換
        # -------------------------
        for t in reversed(tasks):

            if progress.wasCanceled():
                return

            progress.setLabelText(f"置換中：{t['original_name']}")
            progress.setValue(step)
            QCoreApplication.processEvents()

            # 失敗したものはスキップ
            if t["failed"]:
                step += 1
                continue

            # GPKGレイヤ読み込み
            uri = f"{t['gpkg_path']}|layername={t['safe_name']}"
            new_layer = QgsVectorLayer(uri, t["original_name"], "ogr")

            if not new_layer.isValid():
                step += 1
                continue

            # -------------------------
            # スタイル復元
            # -------------------------
            new_layer.setRenderer(t["renderer"])

            if t["labeling"]:
                new_layer.setLabeling(t["labeling"])
            new_layer.setLabelsEnabled(t["labels_enabled"])

            new_layer.setEditFormConfig(t["form_config"])

            for i, setup in enumerate(t["editor_setups"]):
                new_layer.setEditorWidgetSetup(i, setup)

            new_layer.setOpacity(t["opacity"])
            new_layer.setBlendMode(t["blend_mode"])

            parent = t["parent"]
            index = t["index"]

            # 元レイヤ削除
            for child in parent.children():
                if isinstance(child, QgsLayerTreeLayer) and child.layerId() == t["layer_id"]:
                    parent.removeChildNode(child)
                    break

            project.removeMapLayer(t["layer_id"])

            # 同じ位置に追加
            project.addMapLayer(new_layer, False)
            parent.insertChildNode(index, QgsLayerTreeLayer(new_layer))

            step += 1

        # 完了
        progress.setValue(total_steps)

        QMessageBox.information(
            self.iface.mainWindow(),
            "完了",
            "すべて完了しました"
        )