import ast
import pathlib
from typing import List, Set, Tuple

"""
暫存檔清理不可用裸 `except:`

三處清理暫存檔的程式原本寫成裸 `except: pass`，語意是「清不掉就算了，
不要蓋掉真正的失敗原因」——那個意圖是對的，但**裸 except 連
`KeyboardInterrupt` 與 `SystemExit` 都吞得下去**：ETL 跑到一半按 Ctrl+C，
如果正好落在這幾行，中斷會被靜靜丟掉、程式繼續跑下去。

`except OSError:` 表達的才是原本的意圖：只吞檔案系統的錯。

本檔以 AST 掃描而非字串比對——`except:` 這幾個字也會出現在註解與說明字串裡。
"""

PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[1]

# 掃描範圍：`core/` 與 `tasks/` 的全部程式碼
SCAN_DIRS: Tuple[str, ...] = ("core", "tasks", "scripts")


def find_bare_excepts(path: pathlib.Path) -> List[int]:
    """回傳該檔案中裸 `except:` 的行號"""

    tree: ast.AST = ast.parse(path.read_text(), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.type is None
    ]


def test_no_bare_except_anywhere() -> None:
    """
    全庫不得有裸 `except:`

    **這條是 ratchet**：`pyproject.toml` 的 `E722` 已在歸零後整條移除，
    ignore 清單裡沒有它，所以新寫的裸 except 會被 ruff 當場擋下。
    本測試是第二道——它連 `# noqa: E722` 也擋得住。
    """

    offenders: List[str] = []
    for directory in SCAN_DIRS:
        for path in (PROJECT_ROOT / directory).rglob("*.py"):
            for line in find_bare_excepts(path):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line}")

    assert not offenders, (
        "裸 `except:` 連 KeyboardInterrupt 都會吞掉，請改用具名例外：\n"
        + "\n".join(offenders)
    )


def test_temp_file_cleanup_catches_only_filesystem_errors() -> None:
    """
    清暫存檔的三個位置收的是 `OSError`

    釘住的是**意圖**：清不掉暫存檔可以忽略，但別的例外不行。
    改成 `except Exception` 一樣會讓這條紅——那會把程式自己的錯誤也吞掉。
    """

    targets: Tuple[Tuple[str, str], ...] = (
        ("core/pipeline/tw/cleaners/stock_tick_cleaner.py", "unlink"),
        ("core/pipeline/tw/cleaners/stock_tick_cleaner.py", "close"),
        ("core/pipeline/tw/utils/stock_tick_utils.py", "unlink"),
    )

    for relative_path, call_name in targets:
        path: pathlib.Path = PROJECT_ROOT / relative_path
        tree: ast.AST = ast.parse(path.read_text(), filename=str(path))

        handlers: Set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            calls: Set[str] = {
                child.func.attr
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
            }
            if call_name not in calls:
                continue
            for handler in node.handlers:
                if isinstance(handler.type, ast.Name):
                    handlers.add(handler.type.id)

        assert "OSError" in handlers, (
            f"{relative_path} 清理 {call_name}() 的例外處理不是 OSError：{handlers}"
        )
