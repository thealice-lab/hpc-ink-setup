using System;
using System.Drawing;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class InklySshAskpass
{
    [STAThread]
    public static int Main(string[] arguments)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        using (Form dialog = new Form())
        {
            dialog.Text = "HPC SSH sign-in";
            dialog.StartPosition = FormStartPosition.CenterScreen;
            dialog.FormBorderStyle = FormBorderStyle.FixedDialog;
            dialog.MinimizeBox = false;
            dialog.MaximizeBox = false;
            dialog.TopMost = true;
            dialog.AutoSize = true;
            dialog.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            dialog.Font = SystemFonts.MessageBoxFont;

            TableLayoutPanel layout = new TableLayoutPanel();
            layout.AutoSize = true;
            layout.Padding = new Padding(18);
            layout.ColumnCount = 1;
            layout.Dock = DockStyle.Fill;

            Label server = new Label();
            server.Text = "Sign in to " + Environment.GetEnvironmentVariable("INKLY_SSH_TARGET");
            server.AutoSize = true;
            server.MaximumSize = new Size(580, 0);
            layout.Controls.Add(server);

            Label prompt = new Label();
            prompt.Text = arguments.Length > 0 ? arguments[0] : "SSH password or verification response:";
            prompt.AutoSize = true;
            prompt.MaximumSize = new Size(580, 0);
            prompt.Margin = new Padding(0, 12, 0, 12);
            layout.Controls.Add(prompt);

            TextBox response = new TextBox();
            response.UseSystemPasswordChar = true;
            response.Width = 580;
            layout.Controls.Add(response);

            Label privacy = new Label();
            privacy.Text = "Your response is passed to SSH and is not saved to a file.";
            privacy.AutoSize = true;
            privacy.Margin = new Padding(0, 12, 0, 12);
            layout.Controls.Add(privacy);

            FlowLayoutPanel buttons = new FlowLayoutPanel();
            buttons.AutoSize = true;
            buttons.FlowDirection = FlowDirection.RightToLeft;
            buttons.Dock = DockStyle.Fill;

            Button proceed = new Button();
            proceed.Text = "Continue";
            proceed.AutoSize = true;
            proceed.DialogResult = DialogResult.OK;
            buttons.Controls.Add(proceed);

            Button cancel = new Button();
            cancel.Text = "Cancel";
            cancel.AutoSize = true;
            cancel.DialogResult = DialogResult.Cancel;
            buttons.Controls.Add(cancel);

            layout.Controls.Add(buttons);
            dialog.Controls.Add(layout);
            dialog.AcceptButton = proceed;
            dialog.CancelButton = cancel;
            dialog.Shown += delegate { response.Focus(); };

            if (dialog.ShowDialog() != DialogResult.OK)
            {
                response.Clear();
                return 1;
            }

            using (StreamWriter output = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)))
            {
                output.WriteLine(response.Text);
                output.Flush();
            }

            response.Clear();
            return 0;
        }
    }
}
