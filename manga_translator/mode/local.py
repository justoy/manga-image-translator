import json
import os
import gc
from typing import Union, List
import time  

from PIL import Image

from manga_translator import MangaTranslator, Context, TranslationInterrupt, Config
from ..save import save_result
from ..translators import (
    LanguageUnsupportedException,
    dispatch as dispatch_translation,
)
from ..utils import natural_sort, replace_prefix, get_color_name, rgb2hex, get_logger

# 使用专用的local logger
logger = get_logger('local')

# 提示音开关
ENABLE_COMPLETION_SOUND = True

def play_completion_sound():
    """播放完成提示音"""
    try:
        import platform
        if platform.system() == 'Windows':
            import winsound
            # 使用默认系统提示音
            winsound.MessageBeep(-1)
        else:
            # 其他平台使用控制台蜂鸣声
            print('\a', end='', flush=True)
    except Exception as e:
        # 提示音失败不影响主程序
        logger.debug(f'Failed to play completion sound: {e}')

class MangaTranslatorLocal(MangaTranslator):
    def __init__(self, params: dict = None):
        super().__init__(params)
        self.textlines = []
        self.attempts = params.get('attempts', None)
        self.skip_no_text = params.get('skip_no_text', False)
        self.text_output_file = params.get('text_output_file', None)
        self.save_quality = params.get('save_quality', None)
        self.text_regions = params.get('text_regions', None)
        self.save_text_file = params.get('save_text_file', None)
        self.save_text = params.get('save_text', None)
        self.prep_manual = params.get('prep_manual', None)
        self.batch_size = params.get('batch_size', 1)
        self.disable_memory_optimization = params.get('disable_memory_optimization', False)

    async def translate_path(self, path: str, dest: str = None, params: dict[str, Union[int, str]] = None):
        """
        Translates an image or folder (recursively) specified through the path.
        """
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        path = os.path.abspath(os.path.expanduser(path))
        dest = os.path.abspath(os.path.expanduser(dest)) if dest else ''
        params = params or {}
        config_file_path = params.get("config_file", None)

        if config_file_path:
            try:
                with open(config_file_path, 'r', encoding='utf-8') as file:
                    config_content = file.read()
            except Exception as e:
                print("Couldnt read file")
                raise e
            config_extension = os.path.splitext(config_file_path)[1].lower()

            try:
                if config_extension == ".toml":
                    import tomllib
                    config_dict = tomllib.loads(config_content)
                elif config_extension == ".json":
                    config_dict = json.loads(config_content)
                else:
                    raise ValueError("Unsupported configuration file format")
            except Exception as e:
                print("Failed to load configuration file")
                raise e
            config = Config(**config_dict)
        else:
            config = Config()
        # Handle format
        file_ext = params.get('format')
        if params.get('save_quality', 100) < 100:
            if not params.get('format'):
                file_ext = 'jpg'
            elif params.get('format') != 'jpg':
                raise ValueError('--save-quality of lower than 100 is only supported for .jpg files')

        if os.path.isfile(path):
            # Determine destination file path
            if not dest:
                # Use the same folder as the source
                p, ext = os.path.splitext(path)
                _dest = f'{p}-translated.{file_ext or ext[1:]}'
            elif not os.path.basename(dest):
                p, ext = os.path.splitext(os.path.basename(path))
                # If the folders differ use the original filename from the source
                if os.path.dirname(path) != dest:
                    _dest = os.path.join(dest, f'{p}.{file_ext or ext[1:]}')
                else:
                    _dest = os.path.join(dest, f'{p}-translated.{file_ext or ext[1:]}')
            else:
                p, ext = os.path.splitext(dest)
                _dest = f'{p}.{file_ext or ext[1:]}'
            await self.translate_file(path, _dest, params,config)

        elif os.path.isdir(path):
            # Determine destination folder path
            if path[-1] == '\\' or path[-1] == '/':
                path = path[:-1]
            _dest = dest or path + '-translated'
            if os.path.exists(_dest) and not os.path.isdir(_dest):
                raise FileExistsError(_dest)

            # 检查是否使用批量处理
            if self.batch_size > 1:
                await self._translate_folder_batch(path, _dest, params, config, file_ext)
            else:
                # 原有的逐个处理方式
                start_time = time.time()  # 记录开始时间
                translated_count = 0
                for root, subdirs, files in os.walk(path):
                    files = natural_sort(files)
                    dest_root = replace_prefix(root, path, _dest)
                    os.makedirs(dest_root, exist_ok=True)
                    for f in files:
                        if f.lower() == '.thumb':
                            continue

                        file_path = os.path.join(root, f)
                        output_dest = replace_prefix(file_path, path, _dest)
                        p, ext = os.path.splitext(output_dest)
                        output_dest = f'{p}.{file_ext or ext[1:]}'
                        try:
                            if await self.translate_file(file_path, output_dest, params, config):
                                translated_count += 1
                        except Exception as e:
                            logger.error(e)
                            raise e
                
                # 计算总耗时
                total_time = time.time() - start_time
                
                if translated_count == 0:
                    logger.info('No further untranslated files found. Use --overwrite to write over existing translations.')
                else:
                    # 格式化时间显示
                    if total_time >= 3600:  
                        time_str = f"{total_time/3600:.1f} hours"
                    elif total_time >= 60:  
                        time_str = f"{total_time/60:.1f} minutes"
                    else:  
                        time_str = f"{total_time:.1f} seconds"
                    
                    logger.info(f'Done. Translated {translated_count} image{"" if translated_count == 1 else "s"} in {time_str}')
                    logger.info(f'Results saved to: "{_dest}"')
                    try:
                        if ENABLE_COMPLETION_SOUND:
                            play_completion_sound()
                    except Exception as e:
                        logger.debug(f'Failed to play completion sound: {e}')

    async def translate_file(self, path: str, dest: str, params: dict, config: Config):
        if not params.get('overwrite') and os.path.exists(dest):
            logger.info(
                f'Skipping as already translated: "{dest}". Use --overwrite to overwrite existing translations.')
            await self._report_progress('saved', True)
            return True

        logger.info(f'Translating: "{path}"')

        # Turn dict to context to make values also accessible through params.<property>
        params = params or {}
        ctx = Context(**params)

        attempts = 0
        while self.attempts == -1 or attempts < self.attempts + 1:
            if attempts > 0:
                logger.info(f'Retrying translation! Attempt {attempts}'
                            + (f' of {self.attempts}' if self.attempts != -1 else ''))
            try:
                return await self._translate_file(path, dest, config, ctx)

            except TranslationInterrupt:
                break
            except Exception as e:
                if isinstance(e, LanguageUnsupportedException):
                    await self._report_progress('error-lang', True)
                else:
                    await self._report_progress('error', True)
                if not self.ignore_errors and not (self.attempts == -1 or attempts < self.attempts):
                    raise
                else:
                    logger.error(f'{e.__class__.__name__}: {e}',
                                 exc_info=e if self.verbose else None)
            attempts += 1
        return False

    async def _translate_file(self, path: str, dest: str, config: Config, ctx: Context) -> bool:
        if path.endswith('.txt'):
            with open(path, 'r') as f:
                queries = f.read().split('\n')
            translated_sentences = \
                await dispatch_translation(config.translator.translator_gen, queries, self.use_mtpe, ctx,
                                           'cpu' if self._gpu_limited_memory else self.device)
            p, ext = os.path.splitext(dest)
            if ext != '.txt':
                dest = p + '.txt'
            logger.info(f'Saving "{dest}"')
            with open(dest, 'w') as f:
                f.write('\n'.join(translated_sentences))
            return True

        # TODO: Add .gif handler

        else:  # Treat as image
            try:
                img = Image.open(path)
                img.verify()
                img = Image.open(path)
            except Exception:
                logger.warn(f'Failed to open image: {path}')
                return False

            # 直接翻译图片，不再需要传递文件名
            ctx = await self.translate(img, config)
            result = ctx.result

            # TODO
            # Proper way to use the config but for now juste pass what we miss here ton ctx
            # Because old methods are still using for example ctx.gimp_font
            # Not done before because we change the ctx few lines above
            ctx.gimp_font = config.render.gimp_font

            # Save result
            if self.skip_no_text and not ctx.text_regions:
                logger.debug('Not saving due to --skip-no-text')
                return True
            if result:
                logger.info(f'Saving "{dest}"')
                ctx.save_quality = self.save_quality
                save_result(result, dest, ctx)
                await self._report_progress('saved', True)

                if self.save_text or self.save_text_file or self.prep_manual:
                    if self.prep_manual:
                        # Save original image next to translated
                        p, ext = os.path.splitext(dest)
                        img_filename = p + '-orig' + ext
                        img_path = os.path.join(os.path.dirname(dest), img_filename)
                        img.save(img_path, quality=self.save_quality)
                    if ctx.text_regions:
                        self._save_text_to_file(dest, ctx)
                return True
        return False

    def _save_text_to_file(self, image_path: str, ctx: Context):
        cached_colors = []

        def identify_colors(fg_rgb: List[int]):
            idx = 0
            for rgb, _ in cached_colors:
                # If similar color already saved
                if abs(rgb[0] - fg_rgb[0]) + abs(rgb[1] - fg_rgb[1]) + abs(rgb[2] - fg_rgb[2]) < 50:
                    break
                else:
                    idx += 1
            else:
                cached_colors.append((fg_rgb, get_color_name(fg_rgb)))
            return idx + 1, cached_colors[idx][1]

        s = f'\n[{image_path}]\n'
        for i, region in enumerate(ctx.text_regions):
            fore, back = region.get_font_colors()
            color_id, color_name = identify_colors(fore)

            s += f'\n-- {i + 1} --\n'
            s += f'color: #{color_id}: {color_name} (fg, bg: {rgb2hex(*fore)} {rgb2hex(*back)})\n'
            s += f'text:  {region.text}\n'
            s += f'trans: {region.translation}\n'
            for line in region.lines:
                s += f'coords: {list(line.ravel())}\n'
        s += '\n'

        text_output_file = self.text_output_file
        if not text_output_file:
            text_output_file = os.path.splitext(image_path)[0] + '_translations.txt'

        with open(text_output_file, 'a', encoding='utf-8') as f:
            f.write(s)

    async def _translate_folder_batch(self, path: str, dest: str, params: dict, config: Config, file_ext: str):
        """OCR a bounded group, translate its combined text, then save its pages."""
        tasks = []
        for root, dirs, files in os.walk(path):
            dirs[:] = natural_sort(dirs)
            for name in natural_sort(files):
                if name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp')):
                    source = os.path.join(root, name)
                    target = os.path.splitext(replace_prefix(source, path, dest))[0]
                    target += '.' + (file_ext or os.path.splitext(name)[1][1:])
                    if params.get('overwrite') or not os.path.exists(target):
                        tasks.append((source, target))
        logger.info(f'Batch processing: {len(tasks)} pending pages, up to {self.batch_size} pages per request')
        start_time = time.time()
        for offset in range(0, len(tasks), self.batch_size):
            batch = tasks[offset:offset + self.batch_size]
            images = []
            results = []
            try:
                for source, _ in batch:
                    with Image.open(source) as image:
                        images.append(image.copy())
                page_ids = [os.path.splitext(os.path.basename(source))[0] for source, _ in batch]
                logger.info('Processing pages: ' + ', '.join(page_ids))
                results = await self.translate_batch([(image, config) for image in images],
                                                     len(batch), image_names=page_ids)
                if len(results) != len(batch):
                    raise RuntimeError('Batch returned the wrong number of page contexts')
                for image, ctx, (source, target) in zip(images, results, batch):
                    if ctx is None:
                        raise RuntimeError(f'Translation returned no context: {source}')
                    if ctx.text_regions and ctx.result is None:
                        raise RuntimeError(f'Translation returned text but no image: {source}')
                    if self.skip_no_text and not ctx.text_regions:
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    ctx.save_quality = self.save_quality
                    save_result(ctx.result if ctx.result is not None else image, target, ctx)
                    if (self.save_text or self.save_text_file or self.prep_manual) and ctx.text_regions:
                        self._save_text_to_file(target, ctx)
                    if self.prep_manual:
                        image.save(os.path.splitext(target)[0] + '-orig' + os.path.splitext(target)[1])
                    logger.info(f'Saved page: "{target}"')
            finally:
                for image in images:
                    image.close()
                del results
                gc.collect()
        logger.info(f'Batch processing completed: {len(tasks)} pages in {time.time() - start_time:.1f} seconds')
