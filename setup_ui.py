import os
import sys
import json
import subprocess
import tempfile
import threading
import multiprocessing
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

WORKER_TEMPLATE = r'''import os, sys, shutil, json, argparse, re
from setuptools import setup, Extension
from Cython.Build import cythonize

# ─── JS Obfuscator ───────────────────────────────────────────────────────────

def _js_encode_strings(code):
    parts, i = [], 0
    while i < len(code):
        q = code[i]
        if q in ("'", '"'):
            s = i + 1
            j = i + 1
            while j < len(code):
                if code[j] == "\\":
                    j += 2
                elif code[j] == q:
                    parts.append((s, j))
                    i = j + 1
                    break
                else:
                    j += 1
            else:
                i = j
        else:
            i += 1
    for s, e in reversed(parts):
        code = code[:s] + "".join(f"\\u{ord(c):04x}" for c in code[s:e]) + code[e:]
    return code


def _js_encode_identifiers(code):
    apis = [
        "getElementById", "innerText", "createElement", "appendChild",
        "addEventListener", "removeEventListener", "querySelector",
        "querySelectorAll", "classList", "setAttribute", "getAttribute",
        "removeAttribute", "textContent", "innerHTML",
    ]
    for name in apis:
        code = code.replace(name, "".join(f"\\u{ord(c):04x}" for c in name))
    return code


def _html_entity_encode(text):
    return "".join(f"&#{ord(c)};" for c in text)


_JINJA_SPLIT = re.compile(r"(\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\})", re.DOTALL)


def _obfuscate_html_text(html):
    pattern = re.compile(
        r'(<script[^>]*>.*?</script\s*>|<style[^>]*>.*?</style\s*>)|>([^<]+)<',
        re.DOTALL | re.IGNORECASE,
    )
    def replacer(m):
        if m.group(1):
            return m.group(1)
        text = m.group(2)
        parts = _JINJA_SPLIT.split(text)
        encoded = []
        for i, part in enumerate(parts):
            if i % 2 == 0:
                encoded.append(_html_entity_encode(part))
            else:
                encoded.append(part)
        return ">" + "".join(encoded) + "<"

    return pattern.sub(replacer, html)


def obfuscate_html(content):
    try:
        import minify_html
        content = minify_html.minify(content, minify_css=True, minify_js=True)
    except ImportError:
        pass
    return _obfuscate_html_text(content)


def minify_css(content):
    try:
        import rcssmin
        return rcssmin.cssmin(content)
    except ImportError:
        return content


def obfuscate_js(content):
    try:
        import rjsmin
        content = rjsmin.jsmin(content)
    except ImportError:
        pass
    content = _js_encode_strings(content)
    content = _js_encode_identifiers(content)
    junk = 'var _0x0d3d=["",""];(function(){var _0x0=!0,_0x1=!1;while(_0x0){if(_0x1){break}else{_0x1=!_0x1;continue}return}})();'
    return content + junk


def process_web_file(src_path, dst_path, obfuscate_web):
    ext = os.path.splitext(src_path)[1].lower()
    with open(src_path, "r", encoding="utf-8") as f:
        content = f.read()
    if ext == ".html":
        content = obfuscate_html(content) if obfuscate_web else content
    elif ext == ".css":
        content = minify_css(content)
    elif ext == ".js":
        content = obfuscate_js(content)
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write(content)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--src")
    parser.add_argument("--dst")
    args = parser.parse_args()

    args.src = os.path.abspath(args.src)
    args.dst = os.path.abspath(args.dst)

    with open(args.config, "r") as f:
        cfg = json.load(f)

    exclude_dirs = cfg.get("exclude_dirs", [])
    exclude_files = cfg.get("exclude_files", [])
    exclude_exts = cfg.get("exclude_extensions", [])
    keep_files = cfg.get("keep_files", cfg.get("keep_py_files", []))
    keep_file_set = {os.path.normcase(path.replace("\\", "/")) for path in keep_files}
    keep_file_names = {os.path.basename(path) for path in keep_files}
    keep_folders = {
        os.path.normcase(path.replace("\\", "/").strip("/"))
        for path in cfg.get("keep_folders", [])
    }
    obfuscate_web = cfg.get("obfuscate_web", True)
    temp_build = os.path.join(os.environ.get("TEMP", "/tmp"), "pyd_build_tmp")

    def _normalized_relpath(path):
        return os.path.normcase(os.path.relpath(path, args.src).replace("\\", "/").strip("/"))

    def _is_kept_folder(path):
        rel_path = _normalized_relpath(path)
        return any(rel_path == folder or rel_path.startswith(folder + "/") for folder in keep_folders)

    def _is_kept_file(path):
        rel_path = _normalized_relpath(path)
        return rel_path in keep_file_set or os.path.basename(path) in keep_file_names

    try:
        if os.path.exists(temp_build):
            shutil.rmtree(temp_build)
        os.makedirs(temp_build)

        # ── Step 1: Compile .py -> .pyd ──
        py_files = []
        for root, dirs, files in os.walk(args.src):
            dirs[:] = [
                d for d in dirs
                if d not in exclude_dirs and not _is_kept_folder(os.path.join(root, d))
            ]
            for f in files:
                file_path = os.path.join(root, f)
                if f.endswith(".py") and f not in exclude_files and not _is_kept_file(file_path):
                    py_files.append(file_path)

        total = len(py_files)
        print(f"COUNT:{total}", flush=True)
        if total == 0:
            print("NO_PY_FILES", flush=True)
        else:
            src_abs = os.path.abspath(args.src)
            os.chdir(src_abs)
            pyd_count = 0
            for f in py_files:
                rel_path = os.path.relpath(f, src_abs)
                module_name = os.path.splitext(rel_path)[0].replace(os.sep, ".")
                if not module_name.replace(".", "").isidentifier():
                    print(f"SKIP:{rel_path} (invalid module name)", flush=True)
                    continue
                try:
                    ext = Extension(module_name, [f])
                    setup(
                        ext_modules=cythonize([ext], language_level="3", quiet=True),
                        script_args=["build_ext", "--build-lib", temp_build, "--build-temp", os.path.join(temp_build, "tmp")],
                    )
                    print(f"PYD:{rel_path.replace('.py', '.pyd')}", flush=True)
                except Exception as e:
                    print(f"SKIP:{rel_path} ({e})", flush=True)

        print("COPY_RESOURCES", flush=True)

        if os.path.exists(args.dst):
            shutil.rmtree(args.dst)

        web_exts = {".html", ".css", ".js"}

        def _ignore(src_dir, names):
            ignored = set()
            in_kept_folder = _is_kept_folder(src_dir)
            for name in names:
                full = os.path.join(src_dir, name)
                if os.path.isdir(full):
                    if name in exclude_dirs:
                        ignored.add(name)
                else:
                    ext = os.path.splitext(name)[1].lower()
                    if name.endswith((".c", ".pyc")):
                        ignored.add(name)
                    elif any(name.endswith(e.lstrip("*.")) for e in exclude_exts if e.startswith("*")):
                        ignored.add(name)
                    elif name.endswith(".py") and not in_kept_folder and not _is_kept_file(full):
                        ignored.add(name)
                    elif obfuscate_web and not in_kept_folder and ext in web_exts:
                        ignored.add(name)
            return ignored

        shutil.copytree(args.src, args.dst, ignore=_ignore)

        # ── Step 2: Process web files ──
        if obfuscate_web:
            web_count = 0
            for root, dirs, files in os.walk(args.src):
                dirs[:] = [
                    d for d in dirs
                    if d not in exclude_dirs and not _is_kept_folder(os.path.join(root, d))
                ]
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in web_exts:
                        src_file = os.path.join(root, f)
                        rel = os.path.relpath(src_file, args.src)
                        dst_file = os.path.join(args.dst, rel)
                        process_web_file(src_file, dst_file, obfuscate_web)
                        web_count += 1
                        print(f"WEB:{rel}", flush=True)
            if web_count > 0:
                print(f"WEB_COUNT:{web_count}", flush=True)

        # ── Step 3: Copy .pyd files ──
        pyd_count = 0
        for root, dirs, files in os.walk(temp_build):
            for file in files:
                if file.endswith(".pyd"):
                    src_pyd = os.path.join(root, file)
                    real_name = file.split(".")[0] + ".pyd"
                    rel_dir = os.path.relpath(root, temp_build)
                    tgt_dir = os.path.normpath(os.path.join(args.dst, rel_dir))
                    if not os.path.exists(tgt_dir):
                        os.makedirs(tgt_dir)
                    shutil.copy2(src_pyd, os.path.join(tgt_dir, real_name))
                    pyd_count += 1
                    print(f"PYD:{os.path.join(rel_dir, real_name)}", flush=True)

        print("DONE", flush=True)
        return 0

    except Exception as e:
        print(f"ERROR:{e}", flush=True)
        return 1
    finally:
        if os.path.exists(temp_build):
            shutil.rmtree(temp_build)
        cleaned = 0
        for root, dirs, files in os.walk(args.src):
            for f in files:
                if f.endswith(".c"):
                    try:
                        os.remove(os.path.join(root, f))
                        cleaned += 1
                    except:
                        pass
        if cleaned:
            print(f"CLEANUP:{cleaned}", flush=True)

if __name__ == "__main__":
    sys.exit(main())
'''

CONFIG_DEFAULT = {
    "exclude_dirs": [
        "venv", ".git", ".ai", ".cline", ".github", ".opencode", ".vscode",
        ".playwright-mcp", "__pycache__", "build", "dist", "node_modules",
        "alembic", "migrations",
    ],
    "exclude_files": ["setup.py"],
    "exclude_extensions": ["*.pyc", "*.pyo", "*.c", "*.cpp", "*.obj"],
    "keep_files": ["main.py"],
    "keep_folders": ["static", "templates/components"],
    "obfuscate_web": True,
}


class PyToPydConverter:
    def __init__(self, root):
        self.root = root
        self.root.title("Tool Protect FastAPI Source Code - By Dần_VN")
        self.root.geometry("650x320")
        self.root.resizable(False, False)
        self.root.configure(bg="#f0f4f8")

        self.source_dir = tk.StringVar()
        self.target_dir = tk.StringVar()
        self.obfuscate_web = tk.BooleanVar(value=True)
        self.config = self._load_config()

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f0f4f8")
        style.configure("Header.TLabel", background="#f0f4f8", foreground="#1a365d", font=("Segoe UI", 11, "bold"))
        style.configure("TLabel", background="#f0f4f8", foreground="#2d3748", font=("Segoe UI", 10))
        style.configure("TEntry", fieldbackground="#ffffff", foreground="#2d3748", font=("Segoe UI", 10))
        style.configure("Horizontal.TProgressbar", background="#48bb78", troughcolor="#e2e8f0", thickness=12)
        style.map("TButton",
                  background=[("active", "#e2e8f0"), ("!active", "#edf2f7")],
                  foreground=[("active", "#2d3748"), ("!active", "#4a5568")])

        self._build_ui()

    def _build_ui(self):
        root = self.root

        header = tk.Frame(root, bg="#1a365d", height=48)
        header.grid(row=0, column=0, columnspan=3, sticky="ew")
        header.grid_propagate(False)
        tk.Label(header, text="Protect FastAPI Source Code", bg="#1a365d", fg="white",
                 font=("Segoe UI", 13, "bold")).pack(side=tk.LEFT, padx=16, pady=10)
        tk.Label(header, text="v2.0 — By Dần_VN", bg="#1a365d", fg="#a0c4e8",
                 font=("Segoe UI", 9)).pack(side=tk.RIGHT, padx=16, pady=10)

        main = tk.Frame(root, bg="#f0f4f8", padx=20, pady=16)
        main.grid(row=1, column=0, columnspan=3, sticky="nsew")
        root.grid_rowconfigure(1, weight=1)
        root.grid_columnconfigure(1, weight=1)

        lbl_w = 22
        entry_w = 50

        row = 0
        tk.Label(main, text="Thư mục nguồn (Project):", anchor="w",
                 width=lbl_w, bg="#f0f4f8", fg="#2d3748", font=("Segoe UI", 10)).grid(
                 row=row, column=0, padx=(0, 8), pady=8, sticky="w")
        tk.Entry(main, textvariable=self.source_dir, width=entry_w,
                 font=("Consolas", 10), relief="solid", bd=1).grid(
                 row=row, column=1, padx=0, pady=8, sticky="ew")
        tk.Button(main, text="Browse", command=self.browse_source,
                  cursor="hand2", relief="raised", bd=1,
                  bg="#edf2f7", fg="#2d3748", font=("Segoe UI", 9, "bold"),
                  padx=12, pady=1).grid(row=row, column=2, padx=(8, 0), pady=8)

        row = 1
        tk.Label(main, text="Thư mục đích (Encode):", anchor="w",
                 width=lbl_w, bg="#f0f4f8", fg="#2d3748", font=("Segoe UI", 10)).grid(
                 row=row, column=0, padx=(0, 8), pady=8, sticky="w")
        tk.Entry(main, textvariable=self.target_dir, width=entry_w,
                 font=("Consolas", 10), relief="solid", bd=1).grid(
                 row=row, column=1, padx=0, pady=8, sticky="ew")
        tk.Button(main, text="Browse", command=self.browse_target,
                  cursor="hand2", relief="raised", bd=1,
                  bg="#edf2f7", fg="#2d3748", font=("Segoe UI", 9, "bold"),
                  padx=12, pady=1).grid(row=row, column=2, padx=(8, 0), pady=8)

        row = 2
        self.obf_check = tk.Checkbutton(
            main, text="Minify & obfuscate HTML/CSS/JS (bảo vệ mã nguồn web)",
            variable=self.obfuscate_web, bg="#f0f4f8", fg="#2d3748",
            font=("Segoe UI", 9), anchor="w",
            selectcolor="#f0f4f8", activebackground="#f0f4f8"
        )
        self.obf_check.grid(row=row, column=0, columnspan=3, padx=(0, 0), pady=(4, 2), sticky="w")

        sep = ttk.Separator(main, orient="horizontal")
        sep.grid(row=3, column=0, columnspan=3, sticky="ew", pady=8)

        row = 4
        tk.Label(main, text="Trạng thái:", anchor="w", width=lbl_w,
                 bg="#f0f4f8", fg="#2d3748", font=("Segoe UI", 10)).grid(
                 row=row, column=0, padx=(0, 8), pady=(0, 4), sticky="w")
        self.progress = ttk.Progressbar(main, orient="horizontal",
                                         length=400, mode="determinate",
                                         style="Horizontal.TProgressbar")
        self.progress.grid(row=row, column=1, columnspan=2, padx=0, pady=(0, 4), sticky="ew")

        row = 5
        self.progress_label = tk.Label(main, text="Sẵn sàng", anchor="w",
                                        bg="#f0f4f8", fg="#718096",
                                        font=("Segoe UI", 9, "italic"))
        self.progress_label.grid(row=row, column=1, columnspan=2, padx=0, pady=(0, 0), sticky="w")

        sep2 = ttk.Separator(main, orient="horizontal")
        sep2.grid(row=6, column=0, columnspan=3, sticky="ew", pady=8)

        row = 7
        btn_frame = tk.Frame(main, bg="#f0f4f8")
        btn_frame.grid(row=row, column=0, columnspan=3, pady=(0, 4))

        self.btn_run = tk.Button(
            btn_frame, text="CHUYỂN ĐỔI", command=self.start_thread,
            cursor="hand2", relief="raised", bd=0,
            bg="#48bb78", fg="white", width=16,
            font=("Segoe UI", 10, "bold"), padx=8, pady=6
        )
        self.btn_run.pack(side=tk.LEFT, padx=6)

        tk.Button(
            btn_frame, text="CÀI ĐẶT", command=self.open_config,
            cursor="hand2", relief="raised", bd=0,
            bg="#ed8936", fg="white", width=13,
            font=("Segoe UI", 10, "bold"), padx=8, pady=6
        ).pack(side=tk.LEFT, padx=6)

        tk.Button(
            btn_frame, text="ĐÓNG", command=root.quit,
            cursor="hand2", relief="raised", bd=0,
            bg="#e53e3e", fg="white", width=13,
            font=("Segoe UI", 10, "bold"), padx=8, pady=6
        ).pack(side=tk.LEFT, padx=6)

    def _app_dir(self):
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))

    def _config_path(self):
        return os.path.join(self._app_dir(), 'config.json')

    def _load_config(self):
        cfg_path = self._config_path()
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        with open(cfg_path, 'w', encoding='utf-8') as f:
            json.dump(CONFIG_DEFAULT, f, indent=2, ensure_ascii=False)
        return dict(CONFIG_DEFAULT)

    def _save_config(self):
        cfg_path = self._config_path()
        with open(cfg_path, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)

    def open_config(self):
        cfg_path = self._config_path()
        if os.path.exists(cfg_path):
            os.startfile(cfg_path)
        else:
            messagebox.showerror("Loi", "Khong tim thay file cau hinh!")

    def browse_source(self):
        path = filedialog.askdirectory()
        if path:
            self.source_dir.set(os.path.normpath(path))

    def browse_target(self):
        path = filedialog.askdirectory()
        if path:
            self.target_dir.set(os.path.normpath(path))

    def _ui_update(self, enabled=None, status=None, progress_val=None):
        def _apply():
            if enabled is not None:
                self.btn_run.config(state=tk.NORMAL if enabled else tk.DISABLED)
            if status is not None:
                self.progress_label.config(text=status)
            if progress_val is not None:
                self.progress["value"] = progress_val
        self.root.after(0, _apply)

    def _ui_message(self, title, msg, is_error=False):
        def _show():
            if is_error:
                messagebox.showerror(title, msg)
            else:
                messagebox.showinfo(title, msg)
        self.root.after(0, _show)

    def _detect_python(self):
        if not getattr(sys, 'frozen', False):
            return sys.executable
        try:
            result = subprocess.run(
                ['where', 'python'], capture_output=True, text=True, shell=True
            )
            if result.returncode == 0:
                for p in result.stdout.strip().split('\n'):
                    p = p.strip()
                    if p.lower().endswith('python.exe'):
                        return p
        except Exception:
            pass
        return 'python'

    def _check_deps(self, python_exe):
        try:
            proc = subprocess.run(
                [python_exe, '-c', 'import Cython; from setuptools import setup'],
                capture_output=True, text=True
            )
            return proc.returncode == 0
        except Exception:
            return False

    def start_thread(self):
        src = self.source_dir.get()
        dst = self.target_dir.get()

        if not src or not dst:
            self._ui_message("Canh bao", "Vui long chon day du thu muc!", is_error=True)
            return

        src = os.path.normpath(src)
        dst = os.path.normpath(dst)

        if src == dst:
            self._ui_message("Canh bao", "Thu muc nguon va dich khong duoc trung nhau!", is_error=True)
            return

        self.progress["value"] = 0
        self._ui_update(enabled=False, status="Dang kiem tra moi truong...", progress_val=0)
        thread = threading.Thread(target=self._worker_thread, args=(src, dst))
        thread.daemon = True
        thread.start()

    def _worker_thread(self, src, dst):
        worker_path = None
        cfg_path = None
        proc = None

        try:
            python_exe = self._detect_python()

            if not self._check_deps(python_exe):
                self._ui_message(
                    "Thieu thu vien",
                    "Vui long cai dat Cython va setuptools:\n"
                    f"  {python_exe} -m pip install cython setuptools",
                    is_error=True
                )
                return

            self.config["obfuscate_web"] = self.obfuscate_web.get()
            self._save_config()

            cfg_data = {
                'exclude_dirs': self.config.get('exclude_dirs', CONFIG_DEFAULT['exclude_dirs']),
                'exclude_files': self.config.get('exclude_files', CONFIG_DEFAULT['exclude_files']),
                'exclude_extensions': self.config.get('exclude_extensions', CONFIG_DEFAULT['exclude_extensions']),
                'keep_files': self.config.get('keep_files', self.config.get('keep_py_files', CONFIG_DEFAULT['keep_files'])),
                'keep_folders': self.config.get('keep_folders', CONFIG_DEFAULT['keep_folders']),
                'obfuscate_web': self.config.get('obfuscate_web', True),
            }
            cfg_path = os.path.join(tempfile.gettempdir(), 'pyd_config.json')
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(cfg_data, f)

            worker_path = os.path.join(tempfile.gettempdir(), 'pyd_worker.py')
            with open(worker_path, 'w', encoding='utf-8') as f:
                f.write(WORKER_TEMPLATE)

            self._ui_update(status="Dang bien dich...", progress_val=10)

            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            proc = subprocess.Popen(
                [python_exe, worker_path, '--config', cfg_path, '--src', src, '--dst', dst],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW
            )

            stderr_lines = []

            def _collect_stderr():
                for line in proc.stderr:
                    stderr_lines.append(line)

            stderr_thread = threading.Thread(target=_collect_stderr, daemon=True)
            stderr_thread.start()

            file_count = 0
            pyd_count = 0
            web_count = 0
            done = False
            error_msg = None
            no_py = False

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue

                if line.startswith('COUNT:'):
                    file_count = int(line.split(':', 1)[1])
                    if file_count == 0:
                        no_py = True
                    else:
                        self._ui_update(status=f"Da tim thay {file_count} file .py", progress_val=15)

                elif line.startswith('PYD:'):
                    pyd_count += 1
                    pct = 20 + int((pyd_count / file_count) * 40) if file_count else 20
                    self._ui_update(
                        status=f"Dang bien dich: {pyd_count}/{file_count}",
                        progress_val=pct
                    )

                elif line.startswith('WEB:'):
                    web_count += 1

                elif line.startswith('WEB_COUNT:'):
                    wc = line.split(':', 1)[1]
                    self._ui_update(status=f"Dang ma hoa: {wc} file html/css/js", progress_val=75)

                elif line == 'COPY_RESOURCES':
                    self._ui_update(status="Dang sao chep tai nguyen...", progress_val=60)

                elif line.startswith('CLEANUP:'):
                    cleaned = line.split(':', 1)[1]
                    self._ui_update(status=f"Da don dep {cleaned} file .c", progress_val=95)

                elif line == 'NO_PY_FILES':
                    no_py = True

                elif line == 'DONE':
                    done = True
                    self._ui_update(status="Hoan tat!", progress_val=100)
                    break

                elif line.startswith('ERROR:'):
                    done = True
                    error_msg = line.split(':', 1)[1] if ':' in line else line
                    break

            proc.wait()
            stderr_text = ''.join(stderr_lines).strip()

            if done and no_py and web_count == 0:
                self._ui_message("Thong bao", "Khong co file .py va web nao de xu ly!")
            elif done and no_py and web_count > 0:
                self._ui_message("Thanh cong", f"Da ma hoa {web_count} file html/css/js.")
            elif done and not error_msg:
                extra = f"\nDa ma hoa {web_count} file html/css/js" if web_count else ""
                self._ui_message("Thanh cong", f"Da chuyen doi xong!\n{pyd_count} file .pyd{extra}")
            elif error_msg:
                self._ui_message("Loi bien dich", error_msg, is_error=True)
            elif stderr_text:
                self._ui_message("Loi bien dich", stderr_text[:2000], is_error=True)
            elif not done:
                self._ui_update(status="That bai - khong ro nguyen nhan", progress_val=0)

        except Exception as e:
            self._ui_message("Loi he thong", str(e), is_error=True)

        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            for tmp in [worker_path, cfg_path]:
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
            self._ui_update(enabled=True, status="Sẵn sàng", progress_val=0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    root = tk.Tk()
    app = PyToPydConverter(root)
    root.mainloop()
