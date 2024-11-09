import os
import argparse
import sys
from pathlib import Path
import torch
import torch.multiprocessing as mp
import random
import logging

import soundfile as sf
import tqdm
from cached_path import cached_path

from f5_tts.infer.utils_infer import (
    hop_length,
    infer_process,
    load_model,
    load_vocoder,
    preprocess_ref_audio_text,
    remove_silence_for_generated_wav,
    # save_spectrogram,  # Eltávolítva
    target_sample_rate,
)
from f5_tts.model import DiT, UNetT
from f5_tts.model.utils import seed_everything

# Beállítjuk a loggingot
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(processName)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("infer_batch.log")
    ]
)
logger = logging.getLogger(__name__)

class F5TTS:
    def __init__(
        self,
        model_type="F5-TTS",
        ckpt_file="",
        vocab_file="",
        ode_method="euler",
        use_ema=True,
        vocoder_name="vocos",
        local_path=None,
        device=None,
    ):
        # Initialize parameters
        self.final_wave = None
        self.target_sample_rate = target_sample_rate
        self.hop_length = hop_length
        self.seed = -1
        self.mel_spec_type = vocoder_name

        # Set device
        self.device = device or (
            "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        )

        # Load models
        self.load_vocoder_model(vocoder_name, local_path)
        self.load_ema_model(model_type, ckpt_file, vocoder_name, vocab_file, ode_method, use_ema)

    def load_vocoder_model(self, vocoder_name, local_path):
        self.vocoder = load_vocoder(vocoder_name, local_path is not None, local_path, self.device)

    def load_ema_model(self, model_type, ckpt_file, mel_spec_type, vocab_file, ode_method, use_ema):
        if model_type == "F5-TTS":
            if not ckpt_file:
                if mel_spec_type == "vocos":
                    ckpt_file = str(cached_path("hf://SWivid/F5-TTS/F5TTS_Base/model_1200000.safetensors"))
                elif mel_spec_type == "bigvgan":
                    ckpt_file = str(cached_path("hf://SWivid/F5-TTS/F5TTS_Base_bigvgan/model_1250000.pt"))
            model_cfg = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)
            model_cls = DiT
        elif model_type == "E2-TTS":
            if not ckpt_file:
                ckpt_file = str(cached_path("hf://SWivid/E2-TTS/E2TTS_Base/model_1200000.safetensors"))
            model_cfg = dict(dim=1024, depth=24, heads=16, ff_mult=4)
            model_cls = UNetT
        else:
            raise ValueError(f"Unknown model type: {model_type}")

        self.ema_model = load_model(
            model_cls, model_cfg, ckpt_file, mel_spec_type, vocab_file, ode_method, use_ema, self.device
        )

    def export_wav(self, wav, file_wave, remove_silence=False):
        sf.write(file_wave, wav, self.target_sample_rate)

        if remove_silence:
            remove_silence_for_generated_wav(file_wave)

    # def export_spectrogram(self, spect, file_spect):
    #     save_spectrogram(spect, file_spect)  # Eltávolítva

    def infer(
        self,
        ref_file,
        ref_text,
        gen_text,
        file_wave,
        # file_spect=None,  # Eltávolítva
        remove_silence=False,
        speed=1.0,
        nfe_step=32,
        seed=-1,
    ):
        # Validáljuk a speed és nfe_step paramétereket
        if not (0.5 <= speed <= 2.0):
            raise ValueError(f"Invalid speed value: {speed}. Must be between 0.5 and 2.0.")
        if not (16 <= nfe_step <= 64):
            raise ValueError(f"Invalid nfe_step value: {nfe_step}. Must be between 16 and 64.")

        if seed == -1:
            seed = random.randint(0, sys.maxsize)
        seed_everything(seed)
        self.seed = seed

        ref_file, ref_text = preprocess_ref_audio_text(ref_file, ref_text, device=self.device)

        wav, sr, spect = infer_process(
            ref_file,
            ref_text,
            gen_text,
            self.ema_model,
            self.vocoder,
            self.mel_spec_type,
            show_info=logger.info,
            progress=tqdm,
            target_rms=0.1,
            cross_fade_duration=0.15,
            nfe_step=nfe_step,
            cfg_strength=2,
            sway_sampling_coef=-1,
            speed=speed,
            fix_duration=None,
            device=self.device,
        )

        if file_wave is not None:
            self.export_wav(wav, file_wave, remove_silence)

        # if file_spect is not None:
        #     self.export_spectrogram(spect, file_spect)  # Eltávolítva

        return wav, sr, spect

def parse_arguments():
    parser = argparse.ArgumentParser(description="F5-TTS Batch Inference Script with Multi-GPU Support")
    parser.add_argument(
        "-i", "--input_dir", type=str, required=True,
        help="Input directory containing .wav and corresponding .txt files (reference texts)"
    )
    parser.add_argument(
        "-ig", "--input_gen_dir", type=str, required=True,
        help="Input generation directory containing .txt files (generated texts) with the same names as input .wav files"
    )
    parser.add_argument(
        "-o", "--output_dir", type=str, required=True,
        help="Output directory to save generated .wav files"
    )
    parser.add_argument(
        "--remove_silence", action="store_true",
        help="Remove silence from generated audio"
    )
    parser.add_argument(
        "--vocab_file", type=str, required=True,
        help="Path to the vocabulary file (e.g., /path/to/vocab.txt)"
    )
    parser.add_argument(
        "--ckpt_file", type=str, required=True,
        help="Path to the model checkpoint file (e.g., /path/to/model.pt)"
    )
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="Speed of the generated audio (0.5-2.0). Default is 1.0"
    )
    parser.add_argument(
        "--nfe_step", type=int, default=32,
        help="Number of NFE steps (16-64). Default is 32"
    )
    parser.add_argument(
        "--max_workers", type=int, default=None,
        help="Maximum number of parallel workers. Defaults to the number of available GPUs."
    )
    return parser.parse_args()

def process_files(
    wav_files,
    input_dir,
    input_gen_dir,
    output_dir,
    remove_silence,
    vocab_file,
    ckpt_file,
    speed,
    nfe_step,
    device,
):
    try:
        # Inicializáljuk az F5TTS osztályt a megadott paraméterekkel
        f5tts = F5TTS(
            vocab_file=vocab_file,
            ckpt_file=ckpt_file,
            vocoder_name="vocos",
            device=device
        )
        logger.info(f"Initialized F5TTS on device {device}")

        for wav_path in tqdm.tqdm(wav_files, desc=f"Processing on {device}"):
            base_name = wav_path.stem  # Filename without extension

            # Elérési utak a megfelelő .txt fájlokhoz
            ref_txt_path = input_dir / f"{base_name}.txt"
            gen_txt_path = input_gen_dir / f"{base_name}.txt"

            # Kimeneti elérési út
            output_wav_path = output_dir / f"{base_name}.wav"

            # Ellenőrizzük, hogy a kimeneti fájl már létezik-e
            if output_wav_path.exists():
                logger.info(f"Output file {output_wav_path} already exists. Skipping.")
                continue

            # Ellenőrizzük, hogy a referencia és generált szöveges fájlok léteznek-e
            if not ref_txt_path.exists():
                logger.warning(f"Reference text file not found for {wav_path.name}, skipping.")
                continue

            if not gen_txt_path.exists():
                logger.warning(f"Generated text file not found for {wav_path.name}, skipping.")
                continue

            # Beolvassuk a referencia szöveget
            with open(ref_txt_path, "r", encoding="utf-8") as f:
                ref_text = f.read().strip()

            # Beolvassuk a generált szöveget, hozzáadjuk a "... " előtagot és kisbetűssé alakítjuk
            with open(gen_txt_path, "r", encoding="utf-8") as f:
                gen_text = "... " + f.read().strip().lower()

            # Futtatjuk az inference-t
            try:
                f5tts.infer(
                    ref_file=str(wav_path),
                    ref_text=ref_text,
                    gen_text=gen_text,
                    file_wave=str(output_wav_path),
                    # file_spect=None,  # Eltávolítva
                    remove_silence=remove_silence,
                    speed=speed,
                    nfe_step=nfe_step,
                    seed=-1,  # Use random seed
                )
                logger.info(f"Generated audio saved to {output_wav_path}")
            except Exception as e:
                logger.error(f"Error processing {wav_path.name}: {e}", exc_info=True)
    except Exception as e:
        logger.critical(f"Critical error in process on device {device}: {e}", exc_info=True)

def main_worker(
    worker_id,
    chunks,
    input_dir,
    input_gen_dir,
    output_dir,
    remove_silence,
    vocab_file,
    ckpt_file,
    speed,
    nfe_step,
):
    # Meghatározzuk a GPU-t
    device = f"cuda:{worker_id}"
    logger.info(f"Worker {worker_id} using device {device}")

    process_files(
        chunks,
        input_dir,
        input_gen_dir,
        output_dir,
        remove_silence,
        vocab_file,
        ckpt_file,
        speed,
        nfe_step,
        device,
    )

def main():
    args = parse_arguments()

    input_dir = Path(args.input_dir)
    input_gen_dir = Path(args.input_gen_dir)
    output_dir = Path(args.output_dir)

    # Validáljuk a speed és nfe_step paramétereket
    if not (0.5 <= args.speed <= 2.0):
        logger.error(f"Invalid speed value: {args.speed}. Must be between 0.5 and 2.0.")
        sys.exit(1)
    if not (16 <= args.nfe_step <= 64):
        logger.error(f"Invalid nfe_step value: {args.nfe_step}. Must be between 16 and 64.")
        sys.exit(1)

    # Gyűjtsük össze az összes .wav fájlt a bemeneti könyvtárból
    wav_files = list(input_dir.glob("*.wav"))

    if not wav_files:
        logger.error(f"No .wav files found in {input_dir}")
        sys.exit(1)

    # Megvizsgáljuk a rendelkezésre álló GPU-k számát
    num_gpus = torch.cuda.device_count()
    if num_gpus == 0:
        logger.error("No GPUs detected. Exiting.")
        sys.exit(1)

    # Használható munkamenetek számának meghatározása
    max_workers = args.max_workers or num_gpus
    logger.info(f"Number of available GPUs: {num_gpus}")
    logger.info(f"Using {max_workers} parallel workers.")

    # Elosztjuk a fájlokat a munkamenetek között
    chunks = [[] for _ in range(max_workers)]
    for idx, wav_file in enumerate(wav_files):
        chunks[idx % max_workers].append(wav_file)

    # Létrehozzuk a kimeneti könyvtárat, ha nem létezik
    output_dir.mkdir(parents=True, exist_ok=True)

    # Indítjuk a folyamatokat
    processes = []
    for worker_id in range(max_workers):
        p = mp.Process(
            target=main_worker,
            args=(
                worker_id % num_gpus,
                chunks[worker_id],
                input_dir,
                input_gen_dir,
                output_dir,
                args.remove_silence,
                args.vocab_file,
                args.ckpt_file,
                args.speed,
                args.nfe_step,
            )
        )
        p.start()
        processes.append(p)
        logger.info(f"Started process {p.pid} for worker {worker_id} on device cuda:{worker_id % num_gpus}")

    # Várakozunk, amíg minden folyamat befejeződik
    for p in processes:
        p.join()
        if p.exitcode != 0:
            logger.error(f"Process {p.pid} exited with code {p.exitcode}")
        else:
            logger.info(f"Process {p.pid} finished successfully.")

if __name__ == "__main__":
    mp.set_start_method('spawn')  # Biztonságosabb több GPU használat esetén
    main()