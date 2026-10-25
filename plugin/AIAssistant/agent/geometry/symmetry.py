"""Поиск оси вращения: тело вращения ли это, где ось, есть ли осевое отверстие.

Метод (без numpy, по облаку точек Mesh):

* кандидаты в направления оси — оси габаритов файла и главные оси инерции;
* для направления точки делим на BANDS поясов вдоль оси и SECTORS угловых секторов вокруг
  центра; у тела вращения внешний радиус r_max(пояс, сектор) от сектора не зависит.
  Отклонение |r_max - медиана| / медиана, усреднённое по секторам, — асимметрия пояса;
* центр подбираем (алгебраическая подгонка окружности) по всем точкам и по каждому поясу,
  берём лучший — так вал с некруглым кулачком всё равно получает ось вала;
* вдоль выбранной оси лучами ищем материал на оси (сплошная ступица, вал) или сквозное
  отверстие, а точное минимальное расстояние от оси до поверхности даёт диаметр отверстия.
"""

import math
from itertools import islice

from . import vecmath

BANDS = 24
SECTORS = 24
# Пояс считаем «круглым», если его внешний радиус меняется меньше чем на эту долю.
ROUND_BAND_DEV = 0.03
MAX_POINTS = 40000


class AxisFit(object):
    """Результат проверки одной оси-кандидата."""

    def __init__(self, direction, source):
        self.direction = direction
        self.source = source
        self.point = None          # точка на оси (система файла)
        self.score = 1.0           # средняя асимметрия поясов (0 — идеальное тело вращения)
        self.round_fraction = 0.0  # доля длины вдоль оси, занятая круглыми поясами
        self.band_devs = []
        self.h_range = (0.0, 0.0)
        self.r_max = 0.0
        self.rmax_grid = None
        self.rmin_grid = None

    def to_dict(self):
        return {"direction": self.direction, "point": self.point, "source": self.source,
                "score": self.score, "round_fraction": self.round_fraction}


def candidate_axes(mesh):
    """Направления, которые стоит проверить: оси файла и главные оси инерции."""
    result = [((1.0, 0.0, 0.0), "X"), ((0.0, 1.0, 0.0), "Y"), ((0.0, 0.0, 1.0), "Z")]
    if mesh.inertia is not None:
        for index, (_value, vector) in enumerate(vecmath.symmetric_eigen(mesh.inertia)):
            if all(abs(vecmath.dot(vector, existing)) < math.cos(math.radians(3.0)) for existing, _s in result):
                result.append((vector, "P%d" % index))
    return result


def find_axis(mesh):
    """Лучший AxisFit среди кандидатов (с наименьшей асимметрией)."""
    mesh.compute()
    fits = []
    for direction, source in candidate_axes(mesh):
        projection = _Projection(mesh, direction)
        centres = [projection.global_centre(), projection.bbox_centre(),
                   projection.centre_of(mesh.center_of_mass)]
        fit = projection.best_fit(centres, source)
        fits.append((fit, projection))
    fits.sort(key=lambda item: item[0].score)
    # Два самых многообещающих направления уточняем центрами по поясам.
    best = None
    for fit, projection in fits[:2]:
        refined = projection.best_fit(projection.band_centres(), fit.source, current=fit)
        if best is None or refined.score < best.score:
            best = refined
    return best, [fit for fit, _p in fits]


class _Projection(object):
    """Точки облака в координатах (h, u, v) для одного направления оси."""

    def __init__(self, mesh, direction):
        self.mesh = mesh
        self.direction = vecmath.normalize(direction)
        self.e1, self.e2 = vecmath.perpendicular_basis(self.direction)
        ax, ay, az = self.direction
        e1x, e1y, e1z = self.e1
        e2x, e2y, e2z = self.e2
        xs, ys, zs = mesh.samples
        stride = max(1, int(math.ceil(len(xs) / float(MAX_POINTS))))
        self.h = h = []
        self.u = u = []
        self.v = v = []
        for x, y, z in islice(zip(xs, ys, zs), 0, None, stride):
            h.append(x * ax + y * ay + z * az)
            u.append(x * e1x + y * e1y + z * e1z)
            v.append(x * e2x + y * e2y + z * e2z)
        self.h_min = min(h)
        self.h_max = max(h)
        self.length = max(self.h_max - self.h_min, 1e-9)

    def centre_of(self, point):
        return (vecmath.dot(point, self.e1), vecmath.dot(point, self.e2))

    def bbox_centre(self):
        return ((min(self.u) + max(self.u)) / 2.0, (min(self.v) + max(self.v)) / 2.0)

    def global_centre(self):
        return _circle_fit(self.u, self.v) or self.bbox_centre()

    def band_centres(self):
        per_band = [([], []) for _ in range(BANDS)]
        k = BANDS / self.length
        for h, u, v in zip(self.h, self.u, self.v):
            band = min(BANDS - 1, int((h - self.h_min) * k))
            per_band[band][0].append(u)
            per_band[band][1].append(v)
        centres = {}
        for us, vs in per_band:
            if len(us) < 30:
                continue
            centre = _circle_fit(us, vs)
            if centre is None:
                continue
            key = (round(centre[0] / 0.25), round(centre[1] / 0.25))
            centres.setdefault(key, [centre, 0])[1] += 1
        ranked = sorted(centres.values(), key=lambda item: -item[1])
        return [centre for centre, _count in ranked[:5]]

    def best_fit(self, centres, source, current=None):
        best = current
        for centre in centres:
            fit = self.evaluate(centre, source)
            if best is None or fit.score < best.score - 1e-6:
                best = fit
        return best

    def evaluate(self, centre, source):
        cu, cv = centre
        k = BANDS / self.length
        ks = SECTORS / (2.0 * math.pi)
        rmax = [[-1.0] * SECTORS for _ in range(BANDS)]
        rmin = [[float("inf")] * SECTORS for _ in range(BANDS)]
        atan2 = math.atan2
        sqrt = math.sqrt
        for h, u, v in zip(self.h, self.u, self.v):
            du = u - cu
            dv = v - cv
            r = sqrt(du * du + dv * dv)
            band = int((h - self.h_min) * k)
            if band >= BANDS:
                band = BANDS - 1
            sector = int((atan2(dv, du) + math.pi) * ks)
            if sector >= SECTORS:
                sector = SECTORS - 1
            row = rmax[band]
            if r > row[sector]:
                row[sector] = r
            row = rmin[band]
            if r < row[sector]:
                row[sector] = r
        devs = []
        r_all = 0.0
        for row in rmax:
            occupied = sorted(value for value in row if value >= 0.0)
            if not occupied:
                devs.append(None)
                continue
            median = occupied[len(occupied) // 2]
            r_all = max(r_all, occupied[-1])
            if median <= 1e-9:
                devs.append(1.0)
                continue
            dev = sum(abs(value - median) for value in occupied) / (len(occupied) * median)
            # Пустые секторы далеко от оси значат, что здесь тело не круглое.
            missing = 1.0 - len(occupied) / float(SECTORS)
            arc = 2.0 * math.pi * median / SECTORS
            if missing > 0.1 and arc > 3.0 * self.mesh.sample_step:
                dev = max(dev, missing)
            devs.append(dev)
        valid = [dev for dev in devs if dev is not None]
        fit = AxisFit(self.direction, source)
        fit.score = sum(valid) / len(valid) if valid else 1.0
        fit.round_fraction = (sum(1 for dev in valid if dev < ROUND_BAND_DEV) / float(len(valid))) if valid else 0.0
        fit.band_devs = devs
        fit.h_range = (self.h_min, self.h_max)
        fit.r_max = r_all
        fit.rmax_grid = rmax
        fit.rmin_grid = rmin
        fit.centre_uv = centre
        # Точка на оси при самом малом h, в системе файла.
        base = vecmath.add(vecmath.scale(self.e1, cu), vecmath.scale(self.e2, cv))
        fit.point = vecmath.add(base, vecmath.scale(self.direction, self.h_min))
        return fit


def _circle_fit(us, vs):
    """Алгебраическая подгонка окружности (метод Kasa); для концентрических окружностей даёт их центр."""
    n = len(us)
    if n < 3:
        return None
    mu = sum(us) / n
    mv = sum(vs) / n
    suu = suv = svv = sw = suw = svw = 0.0
    for u, v in zip(us, vs):
        u -= mu
        v -= mv
        w = u * u + v * v
        suu += u * u
        suv += u * v
        svv += v * v
        sw += w
        suw += u * w
        svw += v * w
    solution = vecmath.solve3(((suu, suv, 0.0), (suv, svv, 0.0), (0.0, 0.0, float(n))), (suw, svw, sw))
    if solution is None:
        return None
    return (mu + solution[0] / 2.0, mv + solution[1] / 2.0)


# Материал на оси и осевое отверстие

class BoreInfo(object):
    def __init__(self):
        self.axis_material_fraction = 0.0   # доля длины вдоль оси, проходящая внутри материала
        self.through_bore = False
        self.bore_diameter = None           # точный минимум (например, по лыскам)
        self.bore_circle_diameter = None    # типичный диаметр стенки отверстия
        self.roundness = None               # bore_diameter / bore_circle_diameter (1 = round)
        self.support_fraction = 0.0         # доля длины, где стенка касается оправки
        self.crossings = 0
        self.material_at_start = False      # материал на оси у нижнего конца (оттуда может начаться сердечник)
        self.material_at_end = False

    def to_dict(self):
        return dict(self.__dict__)


def analyse_bore(mesh, fit):
    """Проверка лучами вдоль оси и измерение отверстия для AxisFit."""
    info = BoreInfo()
    direction = fit.direction
    e1, e2 = vecmath.perpendicular_basis(direction)
    pu = vecmath.dot(fit.point, e1)
    pv = vecmath.dot(fit.point, e2)
    # Маленький сдвиг, чтобы луч не попадал точно в вершины веерной триангуляции на оси.
    eps = mesh.diagonal * 1e-5
    pu += eps * 0.7071
    pv += eps * 0.3827
    ax, ay, az = direction
    e1x, e1y, e1z = e1
    e2x, e2y, e2z = e2

    # Начальная оценка радиуса отверстия — ближайшая к оси точка облака.
    best = _nearest_sample_radius(mesh, direction, e1, e2, pu, pv) + mesh.sample_step
    hits = []
    sqrt = math.sqrt
    for x0, y0, z0, x1, y1, z1, x2, y2, z2 in zip(*[iter(mesh.coords)] * 9):
        u0 = x0 * e1x + y0 * e1y + z0 * e1z - pu
        u1 = x1 * e1x + y1 * e1y + z1 * e1z - pu
        u2 = x2 * e1x + y2 * e1y + z2 * e1z - pu
        if (u0 > best and u1 > best and u2 > best) or (u0 < -best and u1 < -best and u2 < -best):
            continue
        v0 = x0 * e2x + y0 * e2y + z0 * e2z - pv
        v1 = x1 * e2x + y1 * e2y + z1 * e2z - pv
        v2 = x2 * e2x + y2 * e2y + z2 * e2z - pv
        if (v0 > best and v1 > best and v2 > best) or (v0 < -best and v1 < -best and v2 < -best):
            continue
        d0 = u1 * v2 - u2 * v1    # барицентрические веса начала координат (умноженные на 2*площадь)
        d1 = u2 * v0 - u0 * v2
        d2 = u0 * v1 - u1 * v0
        if (d0 > 0 and d1 > 0 and d2 > 0) or (d0 < 0 and d1 < 0 and d2 < 0):
            total = d0 + d1 + d2
            h0 = x0 * ax + y0 * ay + z0 * az
            h1 = x1 * ax + y1 * ay + z1 * az
            h2 = x2 * ax + y2 * ay + z2 * az
            hits.append((d0 * h0 + d1 * h1 + d2 * h2) / total)
            continue
        for su, sv, tu, tv in ((u0, v0, u1, v1), (u1, v1, u2, v2), (u2, v2, u0, v0)):
            du = tu - su
            dv = tv - sv
            seg2 = du * du + dv * dv
            t = 0.0 if seg2 <= 0 else max(0.0, min(1.0, -(su * du + sv * dv) / seg2))
            qu = su + du * t
            qv = sv + dv * t
            dist = sqrt(qu * qu + qv * qv)
            if dist < best:
                best = dist
    hits.sort()
    info.crossings = len(hits)
    length = max(fit.h_range[1] - fit.h_range[0], 1e-9)
    material = 0.0
    for index in range(0, len(hits) - 1, 2):
        material += hits[index + 1] - hits[index]
    info.axis_material_fraction = min(1.0, material / length)
    if hits:
        near = max(1.0, 0.03 * length)
        info.material_at_start = hits[0] - fit.h_range[0] <= near
        info.material_at_end = fit.h_range[1] - hits[-1] <= near
        return info

    info.through_bore = True
    radius = best
    info.bore_diameter = 2.0 * radius
    # Стенка отверстия — самый внутренний радиус точек в каждом поясе и секторе.
    tolerance = max(0.5, 0.1 * radius)
    supported = 0
    bands = 0
    sector_values = [[] for _ in range(SECTORS)]
    for row in fit.rmin_grid:
        finite = [value for value in row if value != float("inf")]
        if not finite:
            continue
        bands += 1
        if min(finite) <= radius + tolerance:
            supported += 1
            for sector, value in enumerate(row):
                if value <= radius * 1.5 + tolerance:
                    sector_values[sector].append(value)
    info.support_fraction = supported / float(bands) if bands else 0.0
    medians = sorted(sorted(values)[len(values) // 2] for values in sector_values if values)
    if medians:
        circle = medians[len(medians) // 2]
        info.bore_circle_diameter = 2.0 * circle
        info.roundness = min(1.0, radius / circle) if circle > 0 else None
    return info


def _nearest_sample_radius(mesh, direction, e1, e2, pu, pv):
    xs, ys, zs = mesh.samples
    e1x, e1y, e1z = e1
    e2x, e2y, e2z = e2
    best2 = float("inf")
    for x, y, z in zip(xs, ys, zs):
        du = x * e1x + y * e1y + z * e1z - pu
        dv = x * e2x + y * e2y + z * e2z - pv
        r2 = du * du + dv * dv
        if r2 < best2:
            best2 = r2
    return math.sqrt(best2)
