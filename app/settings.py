from PyQt6.QtCore import QSettings


class Settings:
    def __init__(self):
        self._s = QSettings("Maple-FunASR-Desktop", "Maple-FunASR-Desktop")

    @property
    def model_name(self) -> str:
        return self._s.value("model/name", "iic/SenseVoiceSmall")

    @model_name.setter
    def model_name(self, v: str):
        self._s.setValue("model/name", v)

    @property
    def device(self) -> str:
        return self._s.value("device/mode", "auto")

    @device.setter
    def device(self, v: str):
        self._s.setValue("device/mode", v)

    @property
    def vad_max_seg_sec(self) -> int:
        return int(self._s.value("vad/max_seg_sec", 30))

    @vad_max_seg_sec.setter
    def vad_max_seg_sec(self, v: int):
        self._s.setValue("vad/max_seg_sec", v)

    @property
    def batch_size(self) -> int:
        return int(self._s.value("infer/batch_size", 1))

    @batch_size.setter
    def batch_size(self, v: int):
        self._s.setValue("infer/batch_size", v)

    @property
    def batch_size_s(self) -> int:
        return int(self._s.value("infer/batch_size_s", 300))

    @batch_size_s.setter
    def batch_size_s(self, v: int):
        self._s.setValue("infer/batch_size_s", v)

    @property
    def max_speakers(self) -> int:
        return int(self._s.value("infer/max_speakers", 0))

    @max_speakers.setter
    def max_speakers(self, v: int):
        self._s.setValue("infer/max_speakers", v)

    @property
    def theme(self) -> str:
        return self._s.value("app/theme", "auto")

    @theme.setter
    def theme(self, v: str):
        self._s.setValue("app/theme", v)

    @property
    def mirror_source(self) -> str:
        return self._s.value("download/mirror", "modelscope")

    @mirror_source.setter
    def mirror_source(self, v: str):
        self._s.setValue("download/mirror", v)

    @property
    def export_path(self) -> str:
        return self._s.value("export/path", "")

    @export_path.setter
    def export_path(self, v: str):
        self._s.setValue("export/path", v)
