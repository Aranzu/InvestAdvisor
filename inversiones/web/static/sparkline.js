/**
 * Sparkline.js — Generador de mini-gráficos SVG inline (sin dependencias externas).
 *
 * Funciones exportadas vía window.SPARKLINE:
 *   - renderSparklineSVG(data, width, height, withFill)
 *   - renderBarsSVG(data, width, height)
 */
/**
 * Sparkline.js — Generador de mini-gráficos SVG inline (sin dependencias externas).
 * Expone las funciones como globales para uso directo en otros scripts.
 */
(function () {
    "use strict";

    function renderSparklineSVG(data, width, height, withFill) {
        width = width || 80;
        height = height || 24;
        if (!data || data.length < 2) {
            return `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}"><line x1="0" y1="${height/2}" x2="${width}" y2="${height/2}" stroke="#262626" stroke-width="1"/></svg>`;
        }

        const values = data.map(d => d.value);
        const min = Math.min(...values);
        const max = Math.max(...values);
        const range = max - min || 1;

        const padding = 2;
        const usableW = width - padding * 2;
        const usableH = height - padding * 2;

        const points = data.map((d, i) => {
            const x = padding + (i / (data.length - 1)) * usableW;
            const y = padding + (1 - (d.value - min) / range) * usableH;
            return [x, y];
        });

        const isPositive = values[values.length - 1] >= values[0];
        const color = isPositive ? "#16C784" : "#EF4444";

        let pathData = "M " + points.map(p => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L ");
        let fillPath = "";
        if (withFill) {
            fillPath = "M " + points[0][0].toFixed(1) + " " + height +
                " L " + points.map(p => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L ") +
                " L " + points[points.length - 1][0].toFixed(1) + " " + height + " Z";
        }

        let svg = `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" preserveAspectRatio="none">`;
        if (withFill && fillPath) {
            svg += `<path d="${fillPath}" fill="${color}" fill-opacity="0.12" stroke="none"/>`;
        }
        svg += `<path d="${pathData}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`;
        const last = points[points.length - 1];
        svg += `<circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="1.5" fill="${color}"/>`;
        svg += `</svg>`;
        return svg;
    }

    function renderBarsSVG(data, width, height) {
        width = width || 80;
        height = height || 24;
        if (!data || data.length < 1) return "";
        const values = data.map(d => d.value);
        const min = Math.min(...values);
        const max = Math.max(...values);
        const range = max - min || 1;
        const bw = (width - (data.length - 1) * 1) / data.length;

        let svg = `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}">`;
        data.forEach((d, i) => {
            const h = ((d.value - min) / range) * (height - 2) + 1;
            const x = i * (bw + 1);
            const y = height - h;
            const color = i === data.length - 1 ? "#16C784" : "#404040";
            svg += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" fill="${color}"/>`;
        });
        svg += `</svg>`;
        return svg;
    }

    // Exponer también como globales (además de en window.SPARKLINE)
    window.renderSparklineSVG = renderSparklineSVG;
    window.renderBarsSVG = renderBarsSVG;
    window.SPARKLINE = {
        renderSparklineSVG: renderSparklineSVG,
        renderBarsSVG: renderBarsSVG,
    };
})();
