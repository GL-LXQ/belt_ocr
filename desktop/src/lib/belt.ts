// 原样保留 ui/belt_animation.py 的机械几何、材质和绘制顺序。
export const SCENE_VIEW_X = 90
export const SCENE_VIEW_Y = 46
export const SCENE_VIEW_WIDTH = 844
export const SCENE_VIEW_HEIGHT = 402
export const EXTENSION_DURATION_MS = 650
const TAU = Math.PI * 2
type Point = readonly [number, number]

export interface BeltVisualState {
  extension: number
  capturing: boolean
  frequencyListening: boolean
  travel: number
  scanPhase: number
  frequencyPhase: number
}

/**
 * 将相位约束到正向周期。
 * Args:
 *   value: 当前相位。
 *   period: 周期长度。
 * Returns:
 *   2 // 负相位换算后的正值示例
 */
function wrapPhase(value: number, period: number): number {
  return ((value % period) + period) % period
}

/**
 * 转义 SVG 文字内容。
 * Args:
 *   value: 待显示文字。
 * Returns:
 *   "&lt;VISION&gt;" // 转义后的文字示例
 */
function escapeText(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#x27;')
}

class BeltGeometry {
  readonly centerY = 263
  readonly smallRadius = 34
  readonly largeRadius = 66
  readonly depthX = 46
  readonly depthY = -33

  /**
   * 保存两端滚筒的横坐标。
   * Args:
   *   left: 左滚筒坐标。
   *   right: 右滚筒坐标。
   * Returns:
   *   BeltGeometry // 机械几何实例
   */
  constructor(
    readonly left: number,
    readonly right: number,
  ) {}

  /**
   * 计算皮带与滚筒的四个外公切点。
   * Args:
   *   无外部参数。
   * Returns:
   *   [
   *     [313.09, 229.05], // 上左切点
   *     [641.21, 197.31], // 上右切点
   *     [641.21, 328.69], // 下右切点
   *     [313.09, 296.95], // 下左切点
   *   ]
   */
  getTangents(): readonly [Point, Point, Point, Point] {
    const tangentRatio = (this.largeRadius - this.smallRadius) / (this.right - this.left)
    const tangentHeight = Math.sqrt(1 - tangentRatio * tangentRatio)
    return [
      [
        this.left - this.smallRadius * tangentRatio,
        this.centerY - this.smallRadius * tangentHeight,
      ],
      [
        this.right - this.largeRadius * tangentRatio,
        this.centerY - this.largeRadius * tangentHeight,
      ],
      [
        this.right - this.largeRadius * tangentRatio,
        this.centerY + this.largeRadius * tangentHeight,
      ],
      [
        this.left - this.smallRadius * tangentRatio,
        this.centerY + this.smallRadius * tangentHeight,
      ],
    ]
  }

  /**
   * 将正面坐标平移到机械背面。
   * Args:
   *   point: 正面坐标。
   * Returns:
   *   [356, 197] // 背面横纵坐标示例
   */
  projectBackPoint(point: Point): Point {
    return [point[0] + this.depthX, point[1] + this.depthY]
  }
}

class SvgDrawing {
  readonly parts: string[] = []

  /**
   * 追加内部生成的 SVG 片段。
   * Args:
   *   markup: 代码生成的 SVG 内容。
   * Returns:
   *   undefined // 片段已保存
   */
  appendMarkup(markup: string): void {
    this.parts.push(markup)
  }

  /**
   * 追加 SVG 路径。
   * Args:
   *   pathData: 路径指令。
   *   fill: 填充颜色。
   *   stroke: 描边颜色。
   *   width: 描边宽度。
   *   opacity: 透明度。
   * Returns:
   *   undefined // 路径已保存
   */
  drawPath(pathData: string, fill = 'none', stroke = 'none', width = 1, opacity = 1): void {
    this.appendMarkup(
      `<path d="${pathData}" fill="${fill}" stroke="${stroke}" ` +
        `stroke-width="${width}" stroke-linejoin="round" stroke-linecap="round" opacity="${opacity.toFixed(3)}"/>`,
    )
  }

  /**
   * 追加多边形。
   * Args:
   *   points: 顶点数组。
   *   fill: 填充颜色。
   *   stroke: 描边颜色。
   *   width: 描边宽度。
   *   opacity: 透明度。
   * Returns:
   *   undefined // 多边形已保存
   */
  drawPolygon(
    points: readonly Point[],
    fill: string,
    stroke = 'none',
    width = 1,
    opacity = 1,
  ): void {
    const coordinates = points
      .map(([horizontal, vertical]) => `${horizontal.toFixed(3)},${vertical.toFixed(3)}`)
      .join(' ')
    this.appendMarkup(
      `<polygon points="${coordinates}" fill="${fill}" stroke="${stroke}" ` +
        `stroke-width="${width}" stroke-linejoin="round" opacity="${opacity.toFixed(3)}"/>`,
    )
  }

  /**
   * 追加矩形。
   * Args:
   *   horizontalPosition: 横坐标。
   *   verticalPosition: 纵坐标。
   *   rectangleWidth: 宽度。
   *   rectangleHeight: 高度。
   *   fill: 填充颜色。
   *   radius: 圆角半径。
   *   stroke: 描边颜色。
   *   width: 描边宽度。
   * Returns:
   *   undefined // 矩形已保存
   */
  drawRectangle(
    horizontalPosition: number,
    verticalPosition: number,
    rectangleWidth: number,
    rectangleHeight: number,
    fill: string,
    radius = 0,
    stroke = 'none',
    width = 1,
  ): void {
    this.appendMarkup(
      `<rect x="${horizontalPosition.toFixed(3)}" y="${verticalPosition.toFixed(3)}" ` +
        `width="${rectangleWidth.toFixed(3)}" height="${rectangleHeight.toFixed(3)}" rx="${radius}" fill="${fill}" ` +
        `stroke="${stroke}" stroke-width="${width}"/>`,
    )
  }

  /**
   * 追加线段。
   * Args:
   *   startX: 起点横坐标。
   *   startY: 起点纵坐标。
   *   endX: 终点横坐标。
   *   endY: 终点纵坐标。
   *   stroke: 描边颜色。
   *   width: 描边宽度。
   *   opacity: 透明度。
   * Returns:
   *   undefined // 线段已保存
   */
  drawLine(
    startX: number,
    startY: number,
    endX: number,
    endY: number,
    stroke: string,
    width = 1,
    opacity = 1,
  ): void {
    this.drawPath(
      `M${startX.toFixed(3)},${startY.toFixed(3)} L${endX.toFixed(3)},${endY.toFixed(3)}`,
      'none',
      stroke,
      width,
      opacity,
    )
  }

  /**
   * 追加椭圆。
   * Args:
   *   centerX: 中心横坐标。
   *   centerY: 中心纵坐标。
   *   radiusX: 横向半径。
   *   radiusY: 纵向半径。
   *   fill: 填充颜色。
   *   stroke: 描边颜色。
   *   width: 描边宽度。
   *   opacity: 透明度。
   * Returns:
   *   undefined // 椭圆已保存
   */
  drawEllipse(
    centerX: number,
    centerY: number,
    radiusX: number,
    radiusY: number,
    fill: string,
    stroke = 'none',
    width = 1,
    opacity = 1,
  ): void {
    this.appendMarkup(
      `<ellipse cx="${centerX.toFixed(3)}" cy="${centerY.toFixed(3)}" rx="${radiusX.toFixed(3)}" ` +
        `ry="${radiusY.toFixed(3)}" fill="${fill}" stroke="${stroke}" stroke-width="${width}" opacity="${opacity.toFixed(3)}"/>`,
    )
  }

  /**
   * 追加转义后的文字。
   * Args:
   *   horizontalPosition: 横坐标。
   *   verticalPosition: 纵坐标。
   *   value: 显示文字。
   *   size: 字号。
   *   fill: 文字颜色。
   *   weight: 字重。
   *   anchor: 对齐方式。
   *   monospaced: 是否使用等宽字体。
   * Returns:
   *   undefined // 文字已保存
   */
  drawText(
    horizontalPosition: number,
    verticalPosition: number,
    value: string,
    size = 12,
    fill = '#667085',
    weight = 400,
    anchor = 'start',
    monospaced = false,
  ): void {
    const family = monospaced
      ? 'Consolas, DejaVu Sans Mono, monospace'
      : 'Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif'
    this.appendMarkup(
      `<text x="${horizontalPosition.toFixed(3)}" y="${verticalPosition.toFixed(3)}" ` +
        `font-family="${family}" font-size="${size}" fill="${fill}" font-weight="${weight}" ` +
        `text-anchor="${anchor}">${escapeText(value)}</text>`,
    )
  }
}

const SVG_DEFS = `
<defs>
  <linearGradient id="frame" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#f3f6f8"/>
    <stop offset="0.12" stop-color="#dce3e9"/>
    <stop offset="0.7" stop-color="#b2bec9"/>
    <stop offset="1" stop-color="#8e9ba9"/>
  </linearGradient>
  <linearGradient id="post" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#a8b4bf"/>
    <stop offset="0.25" stop-color="#edf1f4"/>
    <stop offset="0.7" stop-color="#c5cfd7"/>
    <stop offset="1" stop-color="#8b98a5"/>
  </linearGradient>
  <linearGradient id="dark" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#566674"/>
    <stop offset="0.45" stop-color="#344451"/>
    <stop offset="1" stop-color="#202d38"/>
  </linearGradient>
  <linearGradient id="rubber" x1="0" y1="0" x2="0.4" y2="1">
    <stop offset="0" stop-color="#202a33"/>
    <stop offset="0.22" stop-color="#414a50"/>
    <stop offset="0.6" stop-color="#262e35"/>
    <stop offset="1" stop-color="#151c23"/>
  </linearGradient>
  <linearGradient id="beltTop" x1="0" y1="0" x2="0.25" y2="1">
    <stop offset="0" stop-color="#50585c"/>
    <stop offset="0.2" stop-color="#343e44"/>
    <stop offset="0.66" stop-color="#283139"/>
    <stop offset="1" stop-color="#1d252c"/>
  </linearGradient>
  <linearGradient id="returnBelt" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#41494e"/>
    <stop offset="1" stop-color="#171f27"/>
  </linearGradient>
  <linearGradient id="rim" x1="0" y1="0" x2="1" y2="0.9">
    <stop offset="0" stop-color="#64727e"/>
    <stop offset="0.22" stop-color="#f8fafb"/>
    <stop offset="0.45" stop-color="#a4b1bb"/>
    <stop offset="0.72" stop-color="#e7edf1"/>
    <stop offset="1" stop-color="#677784"/>
  </linearGradient>
  <radialGradient id="face" cx="0.34" cy="0.23" r="0.9">
    <stop offset="0" stop-color="#f9fbfc"/>
    <stop offset="0.32" stop-color="#d6dee4"/>
    <stop offset="0.7" stop-color="#a2afba"/>
    <stop offset="1" stop-color="#778692"/>
  </radialGradient>
  <linearGradient id="hub" x1="0" y1="0" x2="0.65" y2="1">
    <stop offset="0" stop-color="#fbfcfd"/>
    <stop offset="0.5" stop-color="#bcc7d0"/>
    <stop offset="1" stop-color="#657583"/>
  </linearGradient>
  <radialGradient id="shadow">
    <stop offset="0" stop-color="#324455" stop-opacity="0.19"/>
    <stop offset="0.7" stop-color="#526273" stop-opacity="0.055"/>
    <stop offset="1" stop-color="#526273" stop-opacity="0"/>
  </radialGradient>
  <radialGradient id="glass" cx="0.35" cy="0.3" r="0.8">
    <stop offset="0" stop-color="#81c7e7"/>
    <stop offset="0.3" stop-color="#224f70"/>
    <stop offset="0.7" stop-color="#102536"/>
    <stop offset="1" stop-color="#08131f"/>
  </radialGradient>
  <linearGradient id="beam" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#64b5fa" stop-opacity="0.015"/>
    <stop offset="1" stop-color="#60baff" stop-opacity="0.24"/>
  </linearGradient>
</defs>
`
/**
 * 绘制金属件的正面、侧面和顶面。
 *
 * Args:
 *     drawing: 当前 SVG 绘图内容。
 *     horizontalPosition: 横坐标。
 *     verticalPosition: 纵坐标。
 *     rectangleWidth: 矩形宽度。
 *     rectangleHeight: 矩形高度。
 *     depthX: 背面横向偏移。
 *     depthY: 背面纵向偏移。
 *     front: 正面填充。
 *     side: 侧面填充。
 *     top: 顶面填充。
 *
 * Returns:
 *     返回示例：
 *         undefined // 图形内容已更新
 */
function drawBox(
  drawing: SvgDrawing,
  horizontalPosition: number,
  verticalPosition: number,
  rectangleWidth: number,
  rectangleHeight: number,
  depthX = 13,
  depthY = -9,
  front = 'url(#frame)',
  side = '#8795a1',
  top = '#e3e9ee',
): void {
  // 绘制金属件顶面。
  drawing.drawPolygon(
    [
      [horizontalPosition, verticalPosition],
      [horizontalPosition + depthX, verticalPosition + depthY],
      [horizontalPosition + rectangleWidth + depthX, verticalPosition + depthY],
      [horizontalPosition + rectangleWidth, verticalPosition],
    ],
    top,
    '#a9b5bf',
    0.7,
  )

  // 绘制金属件侧面。
  drawing.drawPolygon(
    [
      [horizontalPosition + rectangleWidth, verticalPosition],
      [horizontalPosition + rectangleWidth + depthX, verticalPosition + depthY],
      [horizontalPosition + rectangleWidth + depthX, verticalPosition + rectangleHeight + depthY],
      [horizontalPosition + rectangleWidth, verticalPosition + rectangleHeight],
    ],
    side,
  )

  // 绘制正面和上沿反光。
  drawing.drawRectangle(
    horizontalPosition,
    verticalPosition,
    rectangleWidth,
    rectangleHeight,
    front,
    0,
    '#91a0ad',
    0.7,
  )
  drawing.drawLine(
    horizontalPosition + 1,
    verticalPosition + 1,
    horizontalPosition + rectangleWidth - 1,
    verticalPosition + 1,
    '#ffffff',
    1.1,
    0.75,
  )
}

/**
 * 绘制螺栓端面。
 *
 * Args:
 *     drawing: 当前 SVG 绘图内容。
 *     horizontalPosition: 横坐标。
 *     verticalPosition: 纵坐标。
 *     smallRadius: 螺栓半径。
 *
 * Returns:
 *     返回示例：
 *         undefined // 图形内容已更新
 */
function drawBolt(
  drawing: SvgDrawing,
  horizontalPosition: number,
  verticalPosition: number,
  smallRadius: number = 3.2,
): void {
  drawing.drawEllipse(
    horizontalPosition,
    verticalPosition,
    smallRadius,
    smallRadius,
    '#778591',
    '#e5ecf1',
    0.7,
  )
  drawing.drawLine(
    horizontalPosition - smallRadius * 0.4,
    verticalPosition - 0.25,
    horizontalPosition + smallRadius * 0.4,
    verticalPosition + 0.25,
    '#263744',
    0.9,
  )
}

/**
 * 绘制底座、支撑脚、滑台、电机和相机支架。
 *
 * Args:
 *     drawing: 当前 SVG 绘图内容。
 *     geometry: 皮带几何参数。
 *
 * Returns:
 *     返回示例：
 *         undefined // 图形内容已更新
 */
function drawFrame(drawing: SvgDrawing, geometry: BeltGeometry): void {
  // 绘制底座下的柔和阴影。
  drawing.drawEllipse(512, 409, 419, 35, 'url(#shadow)')

  // 绘制支撑脚和底座。
  for (const [horizontalPosition, verticalPosition] of [
    [210, 362],
    [825, 362],
    [164, 395],
    [779, 395],
  ] as const) {
    drawing.drawEllipse(
      horizontalPosition + 3,
      verticalPosition + 10,
      25,
      8,
      '#33414c',
      'none',
      1.0,
      0.1,
    )
    drawing.drawEllipse(horizontalPosition, verticalPosition + 5, 17, 6, '#26313c')
    drawing.drawRectangle(horizontalPosition - 11, verticalPosition - 17, 22, 21, 'url(#post)', 2)
    drawing.drawEllipse(horizontalPosition, verticalPosition + 1, 13, 4, '#94a2ae')
    for (const groovePosition of [verticalPosition - 12, verticalPosition - 7] as const) {
      drawing.drawLine(
        horizontalPosition - 10,
        groovePosition,
        horizontalPosition + 10,
        groovePosition,
        '#7c8b98',
        1,
      )
    }
  }

  // 绘制底座横梁和固定螺栓。
  drawBox(drawing, 128, 350, 721, 29, 46, -33)
  drawing.drawRectangle(140, 360, 695, 5, '#637581', 2)
  drawing.drawLine(140, 366, 834, 366, '#eaf0f4', 1.2)
  drawing.drawRectangle(141, 373, 694, 2, '#92a0ac')
  drawing.drawText(467, 372, 'BELT  /  VISION', 8.8, '#4b5c6c', 500, 'start', true)
  for (const horizontalPosition of [153, 306, 688, 822] as const) {
    drawBolt(drawing, horizontalPosition, 355, 2.4)
  }

  // 绘制导轨和随左滚筒移动的张紧滑台。
  drawBox(drawing, 161, 338, 646, 8, 27, -19, 'url(#post)')
  drawing.drawLine(164, 340, 806, 340, '#f7f9fa', 1.4)
  drawing.drawLine(165, 345, 805, 345, '#627585', 1)
  drawing.drawLine(169, 325, 496, 325, '#71818e', 5)
  drawing.drawLine(169, 324, 496, 324, '#dce3e8', 1.3)
  for (let horizontalPosition = 178; horizontalPosition < 494; horizontalPosition += 7) {
    drawing.drawLine(horizontalPosition, 322, horizontalPosition - 2, 328, '#526474', 0.7)
  }

  // 绘制左侧张紧滑台和轴座。
  drawBox(drawing, geometry.left - 43, 327, 86, 13, 26, -19, 'url(#dark)')
  drawBox(drawing, geometry.left - 21, 265, 42, 63, 22, -16, 'url(#post)')
  drawing.drawRectangle(geometry.left - 12, 278, 24, 29, '#8898a5', 3)
  drawBolt(drawing, geometry.left - 29, 332, 2.5)
  drawBolt(drawing, geometry.left + 29, 332, 2.5)
  drawing.drawEllipse(159, 325, 8, 8, 'url(#hub)', '#758593', 1)
  drawing.drawEllipse(159, 325, 3.2, 3.2, '#485b6b')

  // 绘制右轴座和后置电机。
  drawBox(drawing, geometry.right - 28, 300, 61, 40, 30, -21, 'url(#post)')
  drawBox(drawing, geometry.right + 18, 278, 81, 43, 25, -18, 'url(#dark)', '#283d4d', '#617483')
  for (let finIndex = 0; finIndex < 7; finIndex += 1) {
    drawing.drawRectangle(geometry.right + 30 + finIndex * 8, 287, 3, 26, '#203440', 1)
    drawing.drawLine(
      geometry.right + 31 + finIndex * 8,
      287,
      geometry.right + 31 + finIndex * 8,
      311,
      '#6c8190',
      0.7,
    )
  }
  drawing.drawRectangle(geometry.right + 54, 280, 28, 7, '#cad6df', 1)

  // 绘制相机立柱、横臂和线缆。
  drawBox(drawing, 571, 320, 57, 14, 23, -16, 'url(#dark)')
  drawBox(drawing, 584, 69, 23, 252, 13, -9, 'url(#post)')
  drawing.drawRectangle(593, 75, 5, 235, '#8494a1', 2)
  drawing.drawLine(598, 76, 598, 307, '#eff3f6', 1)

  // 绘制相机横臂和连接螺栓。
  drawBox(drawing, 479, 81, 130, 18, 13, -9)
  drawing.drawRectangle(486, 88, 116, 4, '#8e9eab', 1)
  for (const [horizontalPosition, verticalPosition] of [
    [590, 91],
    [599, 91],
    [580, 325],
    [617, 325],
  ] as const) {
    drawBolt(drawing, horizontalPosition, verticalPosition, 2.7)
  }
  drawing.drawPath(
    'M519,91 C518,50 559,48 597,55 C625,59 628,75 627,103 L627,292 Q627,315 639,321',
    'none',
    '#334858',
    3.2,
  )
  drawing.drawPath('M519,91 C518,50 559,48 597,55', 'none', '#8c9ca8', 0.8)
}

/**
 * 生成绕过大小滚筒的皮带轮廓路径。
 *
 * Args:
 *     geometry: 皮带几何参数。
 *
 * Returns:
 *     返回示例：
 *         "M0,0 L1,0 A1,1 0 1 1 1,1 L0,1 A1,1 0 0 1 0,0 Z" // 路径格式
 */
function buildBeltOutline(geometry: BeltGeometry): string {
  const [topLeft, topRight, bottomRight, bottomLeft] = geometry.getTangents()
  const [smallRadius, largeRadius] = [geometry.smallRadius, geometry.largeRadius]
  return (
    `M${topLeft[0].toFixed(3)},${topLeft[1].toFixed(3)} ` +
    `L${topRight[0].toFixed(3)},${topRight[1].toFixed(3)} ` +
    `A${largeRadius},${largeRadius} 0 1 1 ${bottomRight[0].toFixed(3)},${bottomRight[1].toFixed(3)} ` +
    `L${bottomLeft[0].toFixed(3)},${bottomLeft[1].toFixed(3)} ` +
    `A${smallRadius},${smallRadius} 0 0 1 ${topLeft[0].toFixed(3)},${topLeft[1].toFixed(3)} Z`
  )
}

/**
 * 绘制皮带上下表面、绕轮曲面和移动印字。
 *
 * Args:
 *     drawing: 当前 SVG 绘图内容。
 *     geometry: 皮带几何参数。
 *     state: 当前帧的纯视觉参数。
 *     detailed: 是否绘制机械细纹。
 *
 * Returns:
 *     返回示例：
 *         undefined // 图形内容已更新
 */
function drawBeltShell(
  drawing: SvgDrawing,
  geometry: BeltGeometry,
  state: BeltVisualState,
  detailed: boolean,
): void {
  const [topLeft, topRight, bottomRight, bottomLeft] = geometry.getTangents()
  drawing.appendMarkup(`<g transform="translate(${geometry.depthX},${geometry.depthY})">`)
  drawing.drawPath(buildBeltOutline(geometry), 'none', '#343e47', 7)
  drawing.appendMarkup('</g>')
  drawing.drawPolygon(
    [
      bottomLeft,
      bottomRight,
      geometry.projectBackPoint(bottomRight),
      geometry.projectBackPoint(bottomLeft),
    ],
    'url(#returnBelt)',
  )

  // 绘制两端绕轮曲面。
  const tangentRatio =
    (geometry.largeRadius - geometry.smallRadius) / (geometry.right - geometry.left)
  const topAngle = Math.atan2(-Math.sqrt(1 - tangentRatio * tangentRatio), -tangentRatio)
  const bottomAngle = -topAngle
  for (const [centerX, radius, start, end] of [
    [geometry.right, geometry.largeRadius, topAngle, bottomAngle],
    [geometry.left, geometry.smallRadius, bottomAngle, topAngle + 2 * Math.PI],
  ] as const) {
    const points: Point[] = Array.from(
      { length: 29 },
      (_, index) =>
        [
          centerX + radius * Math.cos(start + ((end - start) * index) / 28),
          geometry.centerY + radius * Math.sin(start + ((end - start) * index) / 28),
        ] as Point,
    )
    drawing.drawPolygon(
      [
        ...points,
        ...[...points].reverse().map((frontPoint) => geometry.projectBackPoint(frontPoint)),
      ],
      'url(#rubber)',
    )
    if (detailed) {
      const phase = state.travel / radius

      // 按滚筒半径计算曲面纹理相位。
      const count = radius > 40 ? 34 : 19
      for (let index = 0; index < count; index += 1) {
        const angle = wrapPhase((index * TAU) / count + phase - start, TAU) + start
        if (angle <= end) {
          const frontPoint: Point = [
            centerX + radius * Math.cos(angle),
            geometry.centerY + radius * Math.sin(angle),
          ]
          const backPoint = geometry.projectBackPoint(frontPoint)
          drawing.drawLine(
            frontPoint[0],
            frontPoint[1],
            backPoint[0],
            backPoint[1],
            '#a4adb3',
            0.65,
            0.1,
          )
        }
      }
    }
  }
  drawing.drawPolygon(
    [topLeft, topRight, geometry.projectBackPoint(topRight), geometry.projectBackPoint(topLeft)],
    'url(#beltTop)',
    '#1f2931',
    0.6,
  )
  drawing.drawLine(
    ...geometry.projectBackPoint(topLeft),
    ...geometry.projectBackPoint(topRight),
    '#7d878e',
    1,
    0.7,
  )

  // 将印字坐标映射到皮带上表面。
  const length = Math.hypot(topRight[0] - topLeft[0], topRight[1] - topLeft[1])
  const [surfaceDirectionX, surfaceDirectionY] = [
    (topRight[0] - topLeft[0]) / length,
    (topRight[1] - topLeft[1]) / length,
  ]
  const depth = Math.hypot(geometry.depthX, geometry.depthY)
  const [depthDirectionX, depthDirectionY] = [-geometry.depthX / depth, -geometry.depthY / depth]
  const [originX, originY] = geometry.projectBackPoint(topLeft)
  drawing.appendMarkup(
    `<g transform="matrix(${surfaceDirectionX.toFixed(6)},${surfaceDirectionY.toFixed(6)},` +
      `${depthDirectionX.toFixed(6)},${depthDirectionY.toFixed(6)},${originX.toFixed(3)},${originY.toFixed(3)})">`,
  )
  for (const texturePosition of [6, 13, depth - 13, depth - 6] as const) {
    drawing.drawLine(3, texturePosition, length - 3, texturePosition, '#a7afb3', 0.7, 0.2)
  }
  if (detailed) {
    for (let index = -1; index < Math.trunc(length / 9) + 1; index += 1) {
      const horizontalPosition = index * 9 + wrapPhase(state.travel, 9)
      if (2 < horizontalPosition && horizontalPosition < length - 2) {
        drawing.drawLine(horizontalPosition, 3, horizontalPosition, depth - 3, '#c3cacd', 0.6, 0.11)
      }
    }
  }

  // 绘制处于皮带上表面范围内的移动印字。
  const lettering = '2378244  VEGA X  5EPJ1152'
  for (let repeat = -1; repeat < 3; repeat += 1) {
    const start = 30 + wrapPhase(state.travel, 390) + repeat * 390
    for (const [index, character] of Array.from(lettering).entries()) {
      const horizontalPosition = start + index * 9.2
      if (12 <= horizontalPosition && horizontalPosition <= length - 18 && character !== ' ') {
        drawing.drawText(
          horizontalPosition,
          depth * 0.64,
          character,
          14,
          '#d9dccf',
          500,
          'start',
          true,
        )
      }
    }
  }
  drawing.appendMarkup('</g>')
  drawing.drawPath(buildBeltOutline(geometry), 'none', '#131c24', 7.5)
  drawing.drawLine(...topLeft, ...topRight, '#606c74', 0.9, 0.6)
  drawing.drawLine(...bottomLeft, ...bottomRight, '#72808a', 0.85, 0.55)
}

/**
 * 绘制金属轮缘、旋转孔位和中心轴。
 *
 * Args:
 *     drawing: 当前 SVG 绘图内容。
 *     centerX: 中心横坐标。
 *     centerY: 中心纵坐标。
 *     radius: 圆角或滚筒半径。
 *     travel: 皮带累计移动距离。
 *     detailed: 是否绘制机械细纹。
 *     isDrive: 是否为驱动滚筒。
 *
 * Returns:
 *     返回示例：
 *         undefined // 图形内容已更新
 */
function drawRoller(
  drawing: SvgDrawing,
  centerX: number,
  centerY: number,
  radius: number,
  travel: number,
  detailed: boolean,
  isDrive: boolean,
): void {
  // 绘制滚筒轮缘和端面。
  const rimRadius = radius - 4
  drawing.drawEllipse(centerX, centerY, rimRadius, rimRadius, 'url(#rim)', '#1f2b35', 1)
  drawing.drawEllipse(
    centerX,
    centerY,
    rimRadius - 4.5,
    rimRadius - 4.5,
    'url(#face)',
    '#ebf0f4',
    0.8,
  )
  drawing.drawEllipse(centerX, centerY, rimRadius * 0.72, rimRadius * 0.72, 'none', '#8a99a5', 1.1)
  if (detailed) {
    for (const radiusFraction of [0.79, 0.83, 0.87, 0.91] as const) {
      drawing.drawEllipse(
        centerX,
        centerY,
        rimRadius * radiusFraction,
        rimRadius * radiusFraction,
        'none',
        '#edf2f6',
        0.6,
        0.46,
      )
    }
  }

  // 按累计位移绘制旋转孔位。
  const rotation = travel / radius
  const count = isDrive ? 6 : 4
  for (let index = 0; index < count; index += 1) {
    const angle = rotation + (index * TAU) / count
    const [horizontalPosition, verticalPosition] = [
      centerX + Math.cos(angle) * rimRadius * 0.53,
      centerY + Math.sin(angle) * rimRadius * 0.53,
    ]
    const hole = rimRadius * (isDrive ? 0.105 : 0.1)
    drawing.drawEllipse(
      horizontalPosition,
      verticalPosition + 0.6,
      hole + 1.1,
      hole + 1.1,
      '#eef3f6',
    )
    drawing.drawEllipse(horizontalPosition, verticalPosition, hole, hole, '#5d6c79', '#8f9fae', 0.6)
    drawing.drawEllipse(
      horizontalPosition - 0.2,
      verticalPosition - 0.8,
      hole * 0.72,
      hole * 0.7,
      '#394c5d',
    )
  }
  if (isDrive) {
    // 绘制随驱动滚筒旋转的反光标记。
    const mark = rotation - 0.6
    const markStart: Point = [
      centerX + Math.cos(mark) * (rimRadius - 3),
      centerY + Math.sin(mark) * (rimRadius - 3),
    ]
    const markEnd: Point = [
      centerX + Math.cos(mark + 0.15) * (rimRadius - 3),
      centerY + Math.sin(mark + 0.15) * (rimRadius - 3),
    ]
    drawing.drawLine(...markStart, ...markEnd, '#eed18b', 3.2)
  }

  // 绘制滚筒轴套和中心轴。
  drawing.drawEllipse(centerX + 1.2, centerY + 1.8, rimRadius * 0.27, rimRadius * 0.27, '#778794')
  drawing.drawEllipse(
    centerX,
    centerY,
    rimRadius * 0.255,
    rimRadius * 0.255,
    'url(#hub)',
    '#f0f4f7',
    0.7,
  )
  const points: Point[] = Array.from(
    { length: 6 },
    (_, index) =>
      [
        centerX + Math.cos(rotation + (index * TAU) / 6) * rimRadius * 0.13,
        centerY + Math.sin(rotation + (index * TAU) / 6) * rimRadius * 0.13,
      ] as Point,
  )
  drawing.drawPolygon(points, '#526675', '#f3f6f8', 0.65)
  drawing.drawEllipse(
    centerX - 0.45,
    centerY - 0.45,
    rimRadius * 0.055,
    rimRadius * 0.055,
    '#273e50',
  )
}

/**
 * 绘制频率传感器和监听光束。
 *
 * Args:
 *     drawing: 当前 SVG 绘图内容。
 *     geometry: 皮带几何参数。
 *     state: 当前帧的纯视觉参数。
 *
 * Returns:
 *     返回示例：
 *         undefined // 图形内容已更新
 */
function drawSensor(drawing: SvgDrawing, geometry: BeltGeometry, state: BeltVisualState): void {
  const [horizontalPosition, verticalPosition] = [geometry.right + 103, geometry.centerY - 41]

  // 绘制传感器支架和光头。
  drawBox(drawing, horizontalPosition + 10, verticalPosition + 7, 9, 111, 7, -5, 'url(#post)')
  drawBox(drawing, horizontalPosition - 4, 333, 35, 8, 13, -9)
  drawBox(
    drawing,
    horizontalPosition - 2,
    verticalPosition - 9,
    34,
    20,
    11,
    -8,
    'url(#dark)',
    '#233746',
    '#657888',
  )
  drawing.drawRectangle(horizontalPosition - 14, verticalPosition - 3, 13, 10, 'url(#post)', 2)
  drawing.drawEllipse(horizontalPosition - 14, verticalPosition + 2, 3.2, 5, '#2a3946')

  // 按监听相位绘制传感器脉冲。
  const active = state.frequencyListening
  const phase = wrapPhase(state.frequencyPhase, TAU)
  const pulse = active && Math.min(phase, TAU - phase) < 0.2
  drawing.drawEllipse(
    horizontalPosition + 21,
    verticalPosition - 3,
    2.2,
    2.2,
    active ? '#4adea1' : '#7e8c98',
  )
  drawing.drawEllipse(
    horizontalPosition - 15,
    verticalPosition + 2,
    1.5,
    2.4,
    active ? '#ff896f' : '#4b5d6c',
  )
  if (active) {
    const [targetX, targetY] = [
      geometry.right + Math.cos(-0.6) * 61,
      geometry.centerY + Math.sin(-0.6) * 61,
    ]
    drawing.drawLine(
      targetX,
      targetY,
      horizontalPosition - 16,
      verticalPosition + 2,
      '#ee8979',
      0.85,
      0.36,
    )
    drawing.drawEllipse(
      targetX,
      targetY,
      pulse ? 2.4 : 1.3,
      pulse ? 2.4 : 1.3,
      '#f6a88a',
      'none',
      1.0,
      pulse ? 0.95 : 0.36,
    )
  }
  drawing.drawPath(
    `M${horizontalPosition + 34},${verticalPosition - 3} ` +
      `Q${horizontalPosition + 45},${verticalPosition + 3} ${horizontalPosition + 35},${verticalPosition + 25} ` +
      `L${horizontalPosition + 35},314 Q${horizontalPosition + 36},335 ${horizontalPosition + 49},337`,
    'none',
    '#405665',
    2.2,
  )
}

/**
 * 绘制工业相机、镜头、线光源和采集光束。
 *
 * Args:
 *     drawing: 当前 SVG 绘图内容。
 *     geometry: 皮带几何参数。
 *     state: 当前帧的纯视觉参数。
 *
 * Returns:
 *     返回示例：
 *         undefined // 图形内容已更新
 */
function drawCamera(drawing: SvgDrawing, geometry: BeltGeometry, state: BeltVisualState): void {
  // 按采集开关绘制光束和扫描线。
  const active = state.capturing
  if (active) {
    const [topLeft, topRight] = geometry.getTangents()
    const surfaceFraction = (510 - topLeft[0]) / (topRight[0] - topLeft[0])
    const front: Point = [510.0, topLeft[1] + surfaceFraction * (topRight[1] - topLeft[1])]
    const backPoint = geometry.projectBackPoint(front)
    drawing.drawPolygon(
      [
        [500, 166],
        [516, 166],
        [backPoint[0] + 6, backPoint[1]],
        [front[0] + 6, front[1]],
        [front[0] - 6, front[1]],
        [backPoint[0] - 6, backPoint[1]],
      ],
      'url(#beam)',
    )
    const pulse = 0.7 + 0.3 * Math.sin(state.scanPhase)
    drawing.drawLine(...front, ...backPoint, '#7ecaff', 8, 0.13 * pulse)
    drawing.drawLine(...front, ...backPoint, '#a5e1ff', 2.0, 0.9)
    for (const shift of [-10, 10] as const) {
      drawing.drawLine(
        front[0] + shift,
        front[1] - 1,
        backPoint[0] + shift,
        backPoint[1] - 1,
        '#75b6e7',
        0.6,
        0.6,
      )
    }
  }

  // 绘制相机安装板和机身。
  drawBox(drawing, 495, 97, 26, 8, 8, -5, 'url(#dark)')
  drawBox(drawing, 486, 106, 45, 39, 13, -9, 'url(#frame)', '#667e90', '#dce7ee')
  drawing.drawRectangle(490, 112, 37, 10, '#2c475d', 1)
  drawing.drawText(508.5, 119.3, 'VISION', 6.2, '#e5eef5', 600, 'middle', true)
  for (let ribPosition = 492; ribPosition < 526; ribPosition += 5) {
    drawing.drawLine(ribPosition, 128, ribPosition, 137, '#8e9ba8', 1.0)
  }
  drawBolt(drawing, 490, 140, 1.5)
  drawBolt(drawing, 527, 140, 1.5)
  drawing.drawEllipse(527, 109, 1.8, 1.8, active ? '#4bdfa1' : '#8799a8')

  // 绘制相机镜头和镜片。
  drawing.drawRectangle(498, 145, 22, 20, 'url(#dark)', 2)
  for (const groovePosition of [149, 153, 157] as const) {
    drawing.drawLine(499, groovePosition, 519, groovePosition, '#8799a6', 0.8)
  }
  drawing.drawEllipse(509, 164, 11, 4.2, '#172b3e', '#8799a7', 0.8)
  drawing.drawEllipse(509, 165, 7.5, 2.6, 'url(#glass)')
  drawing.drawEllipse(506.5, 164, 2.4, 0.8, '#bbdef4', 'none', 1.0, 0.7)

  // 绘制独立线光源和支架。
  drawing.drawPath('M540,137 L551,144 L551,179 L535,179', 'none', '#8396a4', 3)
  drawBox(drawing, 478, 178, 67, 8, 8, -6, 'url(#dark)', '#334d60', '#a9bac7')
  drawing.drawLine(482, 185, 540, 185, active ? '#d9f2ff' : '#a0b5c4', 2.4)
}

/**
 * 按原始机械层次生成透明背景的皮带场景。
 * Args:
 *   state: 当前帧的视觉参数。
 *   instanceId: 当前组件的唯一 SVG 标识前缀。
 *   detailed: 是否绘制机械细纹。
 * Returns:
 *   '<svg xmlns="http://www.w3.org/2000/svg"></svg>' // SVG 文档格式
 */
export function renderBeltSvg(state: BeltVisualState, instanceId: string, detailed = true): string {
  // 根据展开比例计算两端滚筒位置。
  const distance = 330 + 230 * state.extension
  const geometry = new BeltGeometry(480 - distance / 2, 480 + distance / 2)
  const drawing = new SvgDrawing()

  // 建立透明画布和机械材质。
  drawing.appendMarkup(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${SCENE_VIEW_WIDTH}" ` +
      `height="${SCENE_VIEW_HEIGHT}" viewBox="${SCENE_VIEW_X} ${SCENE_VIEW_Y} ${SCENE_VIEW_WIDTH} ${SCENE_VIEW_HEIGHT}" ` +
      'preserveAspectRatio="xMidYMid meet" aria-hidden="true" focusable="false">',
  )
  drawing.appendMarkup(SVG_DEFS)

  // 按前后层次绘制机架、皮带和滚筒。
  drawFrame(drawing, geometry)
  drawBeltShell(drawing, geometry, state, detailed)
  drawRoller(
    drawing,
    geometry.left,
    geometry.centerY,
    geometry.smallRadius,
    state.travel,
    detailed,
    false,
  )
  drawRoller(
    drawing,
    geometry.right,
    geometry.centerY,
    geometry.largeRadius,
    state.travel,
    detailed,
    true,
  )

  // 绘制独立工作的传感器和相机。
  drawSensor(drawing, geometry, state)
  drawCamera(drawing, geometry, state)
  drawing.appendMarkup('</svg>')

  // 为每个实例隔离材质引用，并排除标识中的标记字符。
  const safeId = instanceId.replace(/[^a-zA-Z0-9_-]/g, '_')
  return drawing.parts
    .join('')
    .replaceAll(/id="([a-zA-Z]+)"/g, `id="${safeId}-$1"`)
    .replaceAll(/url\(#([a-zA-Z]+)\)/g, `url(#${safeId}-$1)`)
}

/**
 * 计算与 Qt InOutCubic 相同的展开缓动。
 * Args:
 *   progress: 零到一之间的动画进度。
 * Returns:
 *   0.5 // 进行到一半时的展开比例
 */
export function easeExtension(progress: number): number {
  const fraction = Math.min(1, Math.max(0, progress))
  return fraction < 0.5 ? 4 * fraction ** 3 : 1 - (-2 * fraction + 2) ** 3 / 2
}

/**
 * 按原始每三十毫秒的速度推进独立动画相位。
 * Args:
 *   state: 上一帧的视觉参数。
 *   elapsedMs: 本帧经过的有效毫秒数。
 *   running: 展开已完成且皮带处于运行状态。
 * Returns:
 *   {
 *     extension: 1, // 当前展开比例
 *     capturing: true, // 相机开关
 *     frequencyListening: true, // 传感器开关
 *     travel: 2.8, // 皮带累计位移
 *     scanPhase: 0.15707963267948966, // 相机扫描相位
 *     frequencyPhase: 0.15, // 传感器脉冲相位
 *   }
 */
export function advanceBeltState(
  state: BeltVisualState,
  elapsedMs: number,
  running: boolean,
): BeltVisualState {
  const ticks = elapsedMs / 30
  return {
    ...state,
    travel: state.travel + (running ? 2.8 * ticks : 0),
    scanPhase: state.capturing
      ? wrapPhase(state.scanPhase + 0.025 * TAU * ticks, TAU)
      : state.scanPhase,
    frequencyPhase: state.frequencyListening
      ? state.frequencyPhase + 0.15 * ticks
      : state.frequencyPhase,
  }
}
