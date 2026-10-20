import os
import sys
import json
import subprocess
import tempfile
import threading
import multiprocessing
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ─── Worker script chạy trong subprocess ─────────────────────────────────────
WORKER_TEMPLATE = r'''import os, sys, shutil, json, argparse, re, tempfile
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


def process_web_file(src_path, dst_path):
    ext = os.path.splitext(src_path)[1].lower()
    with open(src_path, "r", encoding="utf-8") as f:
        content = f.read()
    if ext == ".html":
        content = obfuscate_html(content)
    elif ext == ".css":
        content = minify_css(content)
    elif ext == ".js":
        content = obfuscate_js(content)
    os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write(content)


def compile_py_to_pyd(src_path, dst_dir):
    """Compile a single .py file to .pyd and copy to dst_dir."""
    temp_build = tempfile.mkdtemp(prefix="pyd_single_")

    src_abs = os.path.abspath(src_path)
    src_dir = os.path.dirname(src_abs)
    filename = os.path.basename(src_abs)
    module_name = os.path.splitext(filename)[0]

    if not module_name.replace("_", "").isidentifier():
        print(f"SKIP:{filename} (invalid module name)", flush=True)
        shutil.rmtree(temp_build, ignore_errors=True)
        return False

    try:
        os.chdir(src_dir)
        ext = Extension(module_name, [src_abs])
        setup(
            ext_modules=cythonize([ext], language_level="3", quiet=True),
            script_args=["build_ext", "--build-lib", temp_build,
                         "--build-temp", os.path.join(temp_build, "tmp")],
        )
        # Find and copy the .pyd
        for root, dirs, files in os.walk(temp_build):
            for f in files:
                if f.endswith(".pyd"):
                    real_name = f.split(".")[0] + ".pyd"
                    os.makedirs(dst_dir, exist_ok=True)
                    shutil.copy2(os.path.join(root, f), os.path.join(dst_dir, real_name))
                    print(f"PYD:{real_name}", flush=True)
                    return True
        print(f"SKIP:{filename} (no .pyd produced)", flush=True)
        return False
    except Exception as e:
        print(f"SKIP:{filename} ({e})", flush=True)
        return False
    finally:
        shutil.rmtree(temp_build, ignore_errors=True)
        # Clean up .c file generated by Cython
        c_file = os.path.join(src_dir, module_name + ".c")
        if os.path.exists(c_file):
            try:
                os.remove(c_file)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", nargs="+", required=True)
    parser.add_argument("--dst", required=True)
    args = parser.parse_args()

    dst = os.path.abspath(args.dst)
    files = [os.path.abspath(f) for f in args.files]
    total = len(files)
    print(f"COUNT:{total}", flush=True)

    web_exts = {".html", ".css", ".js"}
    ok = 0
    for src_path in files:
        ext = os.path.splitext(src_path)[1].lower()
        fname = os.path.basename(src_path)
        if ext == ".py":
            if compile_py_to_pyd(src_path, dst):
                ok += 1
        elif ext in web_exts:
            dst_path = os.path.join(dst, fname)
            try:
                process_web_file(src_path, dst_path)
                print(f"WEB:{fname}", flush=True)
                ok += 1
            except Exception as e:
                print(f"SKIP:{fname} ({e})", flush=True)
        else:
            # Copy as-is
            try:
                os.makedirs(dst, exist_ok=True)
                shutil.copy2(src_path, os.path.join(dst, fname))
                print(f"COPY:{fname}", flush=True)
                ok += 1
            except Exception as e:
                print(f"SKIP:{fname} ({e})", flush=True)

    print(f"DONE:{ok}/{total}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


# ─── Drag-and-drop helper (tkinterdnd2 nếu có, fallback manual) ──────────────
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False


class DropFileConverter:
    def __init__(self, root):
        self.root = root
        self.root.title("Mã hóa File - Kéo & Thả — By Dần_VN")
        self.root.geometry("700x620")
        self.root.minsize(640, 500)
        self.root.resizable(True, True)
        self.root.configure(bg="#f0f4f8")

        self.target_dir = tk.StringVar()
        self.dropped_files: list[str] = []

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f0f4f8")
        style.configure("TLabel", background="#f0f4f8", foreground="#2d3748", font=("Segoe UI", 10))
        style.configure("Horizontal.TProgressbar", background="#48bb78", troughcolor="#e2e8f0", thickness=12)

        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = self.root

        # 1. Header (Top)
        header = tk.Frame(root, bg="#1a365d", height=48)
        header.pack(side=tk.TOP, fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="Mã hóa File (.py / .html / .css / .js)",
                 bg="#1a365d", fg="white", font=("Segoe UI", 13, "bold")).pack(side=tk.LEFT, padx=16, pady=10)
        tk.Label(header, text="v1.0 — By Dần_VN", bg="#1a365d", fg="#a0c4e8",
                 font=("Segoe UI", 9)).pack(side=tk.RIGHT, padx=16, pady=10)

        # 2. Bottom Panel (Cố định ở dưới cùng để luôn nhìn thấy nút Bắt đầu)
        bottom_panel = tk.Frame(root, bg="#f0f4f8", padx=16, pady=10)
        bottom_panel.pack(side=tk.BOTTOM, fill=tk.X)

        ttk.Separator(bottom_panel, orient="horizontal").pack(fill=tk.X, pady=(0, 8))

        # Progress bar
        prog_frame = tk.Frame(bottom_panel, bg="#f0f4f8")
        prog_frame.pack(fill=tk.X, pady=(0, 4))
        tk.Label(prog_frame, text="Trạng thái:", bg="#f0f4f8",
                 fg="#2d3748", font=("Segoe UI", 10, "bold"), width=12, anchor="w").pack(side=tk.LEFT)
        self.progress = ttk.Progressbar(prog_frame, orient="horizontal",
                                         mode="determinate",
                                         style="Horizontal.TProgressbar")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.progress_label = tk.Label(bottom_panel, text="Sẵn sàng", anchor="w",
                                        bg="#f0f4f8", fg="#718096",
                                        font=("Segoe UI", 9, "italic"))
        self.progress_label.pack(anchor="w", pady=(0, 10))

        # Hàng nút BẮT ĐẦU & ĐÓNG (Nổi bật, không bao giờ bị che)
        btn_action_frame = tk.Frame(bottom_panel, bg="#f0f4f8")
        btn_action_frame.pack(fill=tk.X)

        self.btn_run = tk.Button(
            btn_action_frame, text="▶  BẮT ĐẦU MÃ HÓA", command=self._start,
            cursor="hand2", relief="raised", bd=0,
            bg="#38a169", fg="white", activebackground="#2f855a", activeforeground="white",
            font=("Segoe UI", 11, "bold"), padx=16, pady=8
        )
        self.btn_run.pack(side=tk.LEFT, padx=(0, 10), fill=tk.X, expand=True)

        tk.Button(
            btn_action_frame, text="ĐÓNG", command=self.root.quit,
            cursor="hand2", relief="raised", bd=0,
            bg="#e53e3e", fg="white", activebackground="#c53030", activeforeground="white",
            font=("Segoe UI", 10, "bold"), padx=16, pady=8, width=10
        ).pack(side=tk.RIGHT)

        # 3. Middle Content (Tự co giãn ở giữa)
        main = tk.Frame(root, bg="#f0f4f8", padx=16, pady=10)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # ── Drop zone ──
        drop_lbl = tk.Label(main, text="Kéo & thả file vào khung bên dưới (hoặc nhấp chuột vào khung để chọn file):",
                            bg="#f0f4f8", fg="#4a5568", font=("Segoe UI", 9, "bold"))
        drop_lbl.pack(anchor="w", pady=(0, 4))

        self.drop_frame = tk.Frame(
            main, bg="#ebf8ff", relief="groove", bd=2,
            height=100, cursor="hand2"
        )
        self.drop_frame.pack(fill=tk.X, pady=(0, 6))
        self.drop_frame.pack_propagate(False)

        self.drop_inner = tk.Frame(self.drop_frame, bg="#ebf8ff", cursor="hand2")
        self.drop_inner.place(relx=0.5, rely=0.5, anchor="center")

        self.drop_icon = tk.Label(self.drop_inner, text="📂", bg="#ebf8ff", font=("Segoe UI", 24), cursor="hand2")
        self.drop_icon.pack()
        self.drop_hint = tk.Label(self.drop_inner,
                                  text="Kéo thả file vào đây hoặc nhấp để duyệt file",
                                  bg="#ebf8ff", fg="#2b6cb0", font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.drop_hint.pack()

        # Nhấp chuột vào khung drop cũng mở hộp thoại chọn file
        for widget in (self.drop_frame, self.drop_inner, self.drop_icon, self.drop_hint):
            widget.bind("<Button-1>", lambda e: self._add_files())

        # Register DnD if available
        if HAS_DND:
            for widget in (self.drop_frame, self.drop_inner, self.drop_icon, self.drop_hint):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)

        # File list box
        tk.Label(main, text="Danh sách file cần mã hóa:", bg="#f0f4f8",
                 fg="#4a5568", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(4, 2))

        list_frame = tk.Frame(main, bg="#f0f4f8")
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        scrollbar = tk.Scrollbar(list_frame, orient=tk.VERTICAL)
        self.file_listbox = tk.Listbox(
            list_frame, height=5, yscrollcommand=scrollbar.set,
            font=("Consolas", 9), bg="#ffffff", fg="#2d3748",
            selectbackground="#bee3f8", selectforeground="#1a202c",
            relief="solid", bd=1,
        )
        scrollbar.config(command=self.file_listbox.yview)
        self.file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Buttons row 1: add / remove
        btn_row1 = tk.Frame(main, bg="#f0f4f8")
        btn_row1.pack(fill=tk.X, pady=(0, 8))

        tk.Button(btn_row1, text="➕ Thêm file", command=self._add_files,
                  cursor="hand2", relief="raised", bd=0,
                  bg="#4299e1", fg="white", font=("Segoe UI", 9, "bold"),
                  padx=12, pady=4).pack(side=tk.LEFT, padx=(0, 6))

        tk.Button(btn_row1, text="➖ Xóa đã chọn", command=self._remove_selected,
                  cursor="hand2", relief="raised", bd=0,
                  bg="#718096", fg="white", font=("Segoe UI", 9, "bold"),
                  padx=12, pady=4).pack(side=tk.LEFT, padx=(0, 6))

        tk.Button(btn_row1, text="🗑 Xóa tất cả", command=self._clear_files,
                  cursor="hand2", relief="raised", bd=0,
                  bg="#e53e3e", fg="white", font=("Segoe UI", 9, "bold"),
                  padx=12, pady=4).pack(side=tk.LEFT)

        # Target dir
        dst_frame = tk.Frame(main, bg="#f0f4f8")
        dst_frame.pack(fill=tk.X, pady=(0, 4))

        tk.Label(dst_frame, text="Thư mục lưu kết quả:", bg="#f0f4f8",
                 fg="#2d3748", font=("Segoe UI", 10, "bold"), width=18, anchor="w").pack(side=tk.LEFT)
        tk.Entry(dst_frame, textvariable=self.target_dir,
                 font=("Consolas", 10), relief="solid", bd=1).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        tk.Button(dst_frame, text="Browse...", command=self._browse_target,
                  cursor="hand2", relief="raised", bd=1,
                  bg="#edf2f7", fg="#2d3748", font=("Segoe UI", 9, "bold"),
                  padx=12, pady=2).pack(side=tk.LEFT)

    # ── File management ───────────────────────────────────────────────────────

    def _parse_drop_data(self, data: str) -> list[str]:
        """Parse tkinterdnd2 drop data (handles paths with spaces in braces)."""
        import re
        paths = []
        for token in re.findall(r'\{([^}]+)\}|(\S+)', data):
            p = token[0] or token[1]
            if p:
                paths.append(p)
        return paths

    def _on_drop(self, event):
        paths = self._parse_drop_data(event.data)
        self._add_paths(paths)

    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="Chọn file cần mã hóa",
            filetypes=[
                ("Supported files", "*.py *.html *.css *.js"),
                ("Python files", "*.py"),
                ("Web files", "*.html *.css *.js"),
                ("All files", "*.*"),
            ]
        )
        if paths:
            self._add_paths(list(paths))

    def _add_paths(self, paths: list[str]):
        existing = set(self.dropped_files)
        added = 0
        for p in paths:
            p = os.path.normpath(p)
            if os.path.isfile(p) and p not in existing:
                self.dropped_files.append(p)
                existing.add(p)
                self.file_listbox.insert(tk.END, os.path.basename(p) + "  ←  " + p)
                added += 1
        if added:
            self._update_drop_hint()

    def _remove_selected(self):
        selected = list(self.file_listbox.curselection())
        for idx in reversed(selected):
            self.file_listbox.delete(idx)
            del self.dropped_files[idx]
        self._update_drop_hint()

    def _clear_files(self):
        self.dropped_files.clear()
        self.file_listbox.delete(0, tk.END)
        self._update_drop_hint()

    def _update_drop_hint(self):
        n = len(self.dropped_files)
        if n == 0:
            self.drop_hint.config(text="Chưa có file nào được chọn", fg="#4a90d9")
            self.drop_icon.config(text="📂")
        else:
            self.drop_hint.config(text=f"Đã chọn {n} file", fg="#276749")
            self.drop_icon.config(text="✅")

    def _browse_target(self):
        path = filedialog.askdirectory(title="Chọn thư mục lưu kết quả")
        if path:
            self.target_dir.set(os.path.normpath(path))

    # ── Run ───────────────────────────────────────────────────────────────────

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

    def _start(self):
        if not self.dropped_files:
            self._ui_message("Cảnh báo", "Chưa có file nào được chọn!", is_error=True)
            return
        dst = self.target_dir.get().strip()
        if not dst:
            self._ui_message("Cảnh báo", "Vui lòng chọn thư mục lưu kết quả!", is_error=True)
            return

        self.progress["value"] = 0
        self._ui_update(enabled=False, status="Đang kiểm tra môi trường...", progress_val=0)
        thread = threading.Thread(target=self._worker_thread, args=(list(self.dropped_files), dst))
        thread.daemon = True
        thread.start()

    def _worker_thread(self, files: list[str], dst: str):
        worker_path = None
        proc = None
        try:
            python_exe = self._detect_python()

            # Check if any .py file needs Cython
            has_py = any(f.endswith(".py") for f in files)
            if has_py and not self._check_deps(python_exe):
                self._ui_message(
                    "Thiếu thư viện",
                    "Vui lòng cài đặt Cython và setuptools:\n"
                    f"  {python_exe} -m pip install cython setuptools",
                    is_error=True
                )
                return

            worker_path = os.path.join(tempfile.gettempdir(), 'pyd_single_worker.py')
            with open(worker_path, 'w', encoding='utf-8') as f:
                f.write(WORKER_TEMPLATE)

            self._ui_update(status="Đang xử lý...", progress_val=10)

            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            cmd = [python_exe, worker_path, '--dst', dst, '--files'] + files
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW
            )

            stderr_lines = []

            def _collect_stderr():
                for line in proc.stderr:
                    stderr_lines.append(line)

            stderr_thread = threading.Thread(target=_collect_stderr, daemon=True)
            stderr_thread.start()

            total = len(files)
            done_count = 0
            done = False
            error_msg = None
            ok_count = 0
            skip_list = []

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue

                if line.startswith('COUNT:'):
                    total = int(line.split(':', 1)[1])

                elif line.startswith('PYD:') or line.startswith('WEB:') or line.startswith('COPY:'):
                    done_count += 1
                    pct = 10 + int((done_count / total) * 85) if total else 95
                    tag, name = line.split(':', 1)
                    label = {"PYD": "Biên dịch", "WEB": "Mã hóa web", "COPY": "Sao chép"}.get(tag, tag)
                    self._ui_update(
                        status=f"{label}: {name}  ({done_count}/{total})",
                        progress_val=pct
                    )

                elif line.startswith('SKIP:'):
                    skip_list.append(line.split(':', 1)[1])
                    done_count += 1

                elif line.startswith('DONE:'):
                    done = True
                    parts = line.split(':', 1)[1].split('/')
                    ok_count = int(parts[0])
                    self._ui_update(status="Hoàn tất!", progress_val=100)
                    break

                elif line.startswith('ERROR:'):
                    done = True
                    error_msg = line.split(':', 1)[1] if ':' in line else line
                    break

            proc.wait()
            stderr_text = ''.join(stderr_lines).strip()

            if error_msg:
                self._ui_message("Lỗi", error_msg, is_error=True)
            elif done:
                msg = f"Đã xử lý xong {ok_count}/{total} file.\nKết quả lưu tại:\n{dst}"
                if skip_list:
                    msg += f"\n\nBỏ qua {len(skip_list)} file:\n" + "\n".join(skip_list[:10])
                    if len(skip_list) > 10:
                        msg += f"\n... và {len(skip_list) - 10} file khác"
                self._ui_message("Thành công", msg)
            elif stderr_text:
                self._ui_message("Lỗi biên dịch", stderr_text[:2000], is_error=True)
            else:
                self._ui_update(status="Thất bại - không rõ nguyên nhân", progress_val=0)

        except Exception as e:
            self._ui_message("Lỗi hệ thống", str(e), is_error=True)
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            if worker_path and os.path.exists(worker_path):
                try:
                    os.remove(worker_path)
                except Exception:
                    pass
            self._ui_update(enabled=True, status="Sẵn sàng", progress_val=0)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    multiprocessing.freeze_support()
    if HAS_DND:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    app = DropFileConverter(root)

    if not HAS_DND:
        # Show a gentle notice that native DnD is unavailable
        root.after(300, lambda: messagebox.showinfo(
            "Gợi ý",
            "Để dùng tính năng kéo & thả file trực tiếp, hãy cài:\n"
            "  pip install tkinterdnd2\n\n"
            "Hiện tại bạn vẫn có thể dùng nút '➕ Thêm file' để chọn file."
        ))

    root.mainloop()


if __name__ == "__main__":
    main()
