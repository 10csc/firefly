// 自绘下拉组件（ui_select）：替代原生 <select> 的系统弹窗
// 背景：安卓 WebView 的 <select> 点击弹系统级白色选项弹窗，页面 CSS 管不到（图1 实测）。
// 方案：原生 select 保留为值存储（display:none，所有既有 change 监听/取值代码零改动），
// 视觉层换成暗色「按钮 + 自绘弹层」。外观/键盘/触摸/点外关闭齐全。
// 用法：uiSelectEnhance(root=document) 扫描 select[data-ui] 或调用方指定选择器。

const _UI_SEL_KEY = "data-ui-select-enhanced";

function _closeAllPopups(except) {
    document.querySelectorAll(".ui-select-pop.show").forEach(p => {
        if (p !== except) p.classList.remove("show");
    });
}

// 全局关闭：点外 / Esc / 滚动（弹层跟随是 fixed，滚动时直接关，防错位）
document.addEventListener("pointerdown", e => {
    if (!e.target.closest(".ui-select-pop") && !e.target.closest(".ui-select-btn")) _closeAllPopups();
}, { capture: true });
document.addEventListener("keydown", e => { if (e.key === "Escape") _closeAllPopups(); });
window.addEventListener("scroll", () => _closeAllPopups(), { capture: true, passive: true });

export function uiSelectEnhance(root) {
    (root || document).querySelectorAll("select").forEach(sel => {
        if (sel.hasAttribute(_UI_SEL_KEY)) return;
        sel.setAttribute(_UI_SEL_KEY, "1");
        sel.classList.add("ui-select-native");

        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ui-select-btn";
        btn.setAttribute("aria-haspopup", "listbox");
        const label = document.createElement("span");
        label.className = "ui-select-label";
        const arrow = document.createElement("span");
        arrow.className = "ui-select-arrow";
        arrow.textContent = "▾";
        btn.append(label, arrow);

        const pop = document.createElement("div");
        pop.className = "ui-select-pop";
        pop.setAttribute("role", "listbox");

        const syncLabel = () => {
            const opt = sel.selectedOptions && sel.selectedOptions[0];
            label.textContent = opt ? opt.textContent : "";
        };
        const rebuild = () => {
            pop.innerHTML = "";
            [...sel.options].forEach(o => {
                const item = document.createElement("button");
                item.type = "button";
                item.className = "ui-select-opt" + (o.value === sel.value ? " on" : "");
                item.setAttribute("role", "option");
                item.textContent = o.textContent;
                item.addEventListener("click", () => {
                    sel.value = o.value;
                    sel.dispatchEvent(new Event("change", { bubbles: true }));
                    syncLabel(); rebuild(); _closeAllPopups();
                });
                pop.appendChild(item);
            });
        };
        btn.addEventListener("click", () => {
            const willOpen = !pop.classList.contains("show");
            _closeAllPopups();
            if (willOpen) {
                rebuild(); syncLabel();
                // 定位：fixed 贴按钮下缘；下方空间不足则翻上
                const r = btn.getBoundingClientRect();
                pop.style.minWidth = r.width + "px";
                pop.style.left = r.left + "px";
                pop.style.top = "";
                pop.style.bottom = "";
                pop.classList.add("show");
                const ph = pop.offsetHeight;
                if (r.bottom + ph + 8 > innerHeight && r.top - ph - 8 > 0) {
                    pop.style.top = (r.top - ph - 6) + "px";
                } else {
                    pop.style.top = (r.bottom + 6) + "px";
                }
            }
        });
        syncLabel();
        sel.insertAdjacentElement("afterend", btn);
        document.body.appendChild(pop);
        // 弹层随按钮销毁（本项目的 select 都是长驻元素，不销毁；防御性留个口）
        sel._uiSelectPopup = pop;
        sel._uiSelectBtn = btn;
        sel._uiSelectSync = () => { syncLabel(); rebuild(); };
    });
}

// 动态重建内容的 select（如 provider-select 会被 _renderProviderSelect 重写 options）：
// 调用方在重写后调 sel._uiSelectSync() 即可（settings.js 已接）。
