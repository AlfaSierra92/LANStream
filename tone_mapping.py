"""Build HDR-to-SDR filter plans without downloading media or guessing from names."""
from dataclasses import dataclass
import base64


class ToneMappingError(ValueError):
    pass


@dataclass(frozen=True)
class ToneMappingPlan:
    label: str = 'Off · SDR source'
    filters: tuple[str, ...] = ()
    required: frozenset[str] = frozenset()

    @property
    def active(self):
        return bool(self.filters)


# One generated white HEVC Main 10 frame (256x256), with PQ/BT.2020,
# mastering-display and content-light SEI. Used only for cached capability tests.
# Keeping this tiny fixture here avoids files and a runtime x265 dependency.
HDR10_PROBE = base64.b64decode(
    'AAAAAUABDAH//wIgAAADAJAAAAMAAAMAPJWYCQAAAAFCAQECIAAAAwCQAAADAAADADygCAgEBNllZpJMrwFqEiASCAAAAwAIAAADAAhAAAAAAUQBwXK0YkAAAAFOAZAEA+gBkIAAAAFOAYkYM8KGxB1MC7iE0D6APRNAQgCYloAAAAMAAYAAAAEoAa8TIWKDQOZo47/89M/H9kAtWJjtrvgEPKXvcrgFwB8kXJMChCpAC0iHAAADACag'
)
HDR10_VIDEO = {'color_transfer': 'smpte2084', 'color_primaries': 'bt2020', 'color_space': 'bt2020nc'}


def tone_mapping_plan(video, backend):
    video = video or {}
    transfer = video.get('color_transfer')
    base = ''
    for data in video.get('side_data_list', []):
        if 'dv_profile' not in data:
            continue
        profile, compatible = data.get('dv_profile'), data.get('dv_bl_signal_compatibility_id')
        if profile == 7 or (profile == 8 and compatible == 1):
            transfer, base = 'smpte2084', 'Dolby Vision HDR10 base layer'
        elif profile == 8 and compatible == 4:
            transfer, base = 'arib-std-b67', 'Dolby Vision HLG base layer'
        elif profile == 8 and compatible == 2:
            return ToneMappingPlan('Off · Dolby Vision SDR base layer')
        else:
            raise ToneMappingError('This Dolby Vision profile has no supported HDR10/HLG base layer. Use an HDR10 version or Original on a compatible client.')
    if transfer not in {'smpte2084', 'arib-std-b67'}:
        return ToneMappingPlan()
    source = base or ('HDR10/PQ' if transfer == 'smpte2084' else 'HLG')
    # Accept known HDR gamuts, and restore missing tags using the HDR base-layer
    # defaults. Never interpolate arbitrary probe strings into a filter graph.
    primaries = video.get('color_primaries')
    matrix = video.get('color_space')
    if primaries in {None, '', 'unknown', 'unspecified'}:
        primaries = 'bt2020'
    if matrix in {None, '', 'unknown', 'unspecified'}:
        matrix = 'bt2020nc'
    if primaries not in {'bt2020', 'bt709', 'smpte432'} or matrix not in {'bt2020nc', 'bt2020c', 'bt709'}:
        raise ToneMappingError('Unsupported HDR color primaries or matrix; refusing an incorrect SDR conversion.')
    if backend == 'vaapi':
        if transfer != 'smpte2084':
            raise ToneMappingError('VAAPI tone mapping supports HDR10/PQ input here, not HLG. Software fallback is disabled; use an HDR10 source or Original on a compatible client.')
        return ToneMappingPlan(
            f'{source} → SDR BT.709 · VAAPI',
            (f'setparams=color_primaries={primaries}:color_trc={transfer}:colorspace={matrix}',
             'tonemap_vaapi=format=nv12:p=bt709:t=bt709:m=bt709'),
            frozenset({'setparams', 'tonemap_vaapi', 'scale_vaapi', 'sidedata'}),
        )
    return ToneMappingPlan(
        f'{source} → SDR BT.709 · CPU Hable',
        (f'zscale=transferin={transfer}:primariesin={primaries}:matrixin={matrix}:t=linear:npl=100',
         'format=gbrpf32le', 'zscale=p=bt709', 'tonemap=tonemap=hable:desat=2',
         'zscale=t=bt709:m=bt709:r=tv'),
        frozenset({'zscale', 'format', 'tonemap', 'sidedata'}),
    )
